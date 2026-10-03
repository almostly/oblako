"""oblako dashboard API: exposes local services to the Cloudscape frontend."""

import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from oblako.services.platform import Oblako
from oblako.services import sfn_templates
from oblako import config

oblako = Oblako()
# The built React app lives alongside this module at oblako/dashboard/frontend/dist.
DIST_DIR = Path(__file__).parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bring up the Lambda shim so Step Functions lambda:invoke tasks (e.g. the
    # Bedrock prompt-chain -> local model) can run live against oblako services.
    try:
        from oblako.engines import lambda_shim

        lambda_shim.start_in_thread()
    except Exception:  # dashboard still works without live SFN runs
        pass
    yield


app = FastAPI(title="oblako", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------------------------
# Services
# -----------------------------------------------------------------------------------------------
@app.get("/api/config")
def get_config():
    """Return the active region + account and the selectable regions."""
    return {
        "region": config.region(),
        "accountId": config.account_id(),
        "regions": config.REGIONS,
    }


@app.post("/api/config")
def set_config(body: dict):
    """Set the active region (and optionally account); live clients pick it up."""
    if body.get("region"):
        config.set_region(body["region"])
        # Services that cached region at construction follow the new selection.
        for svc in (oblako.bedrock, oblako.redshift, oblako.rds, oblako.cloudformation):
            svc.region = config.region()
    if body.get("accountId"):
        config.set_account(body["accountId"])
    return {"region": config.region(), "accountId": config.account_id()}


@app.get("/api/services")
def get_services():
    """Service status overview."""
    status = oblako.status()
    services = []
    services.extend(
        {"name": name, "status": state, "type": _service_type(name)}
        for name, state in status.items()
    )
    return {"services": services}


def _service_type(name: str) -> str:
    types = {
        "bedrock": "Bedrock (LLM)",
        "opensearch": "Knowledge Bases",
        "redshift": "Redshift",
        "rds": "RDS / Aurora",
        "s3proxy": "S3",
        "dynamodb": "DynamoDB",
        "stepfunctions": "Step Functions",
        "sagemaker": "SageMaker",
        "lambda": "Lambda",
        "ec2": "EC2",
        "appconfig": "AppConfig",
    }
    return types.get(name, name)


# -----------------------------------------------------------------------------------------------
# S3
# -----------------------------------------------------------------------------------------------
@app.get("/api/s3/buckets")
def list_buckets():
    s3 = oblako.s3.get_client()
    try:
        resp = s3.list_buckets()
        return {
            "buckets": [
                {"Name": b["Name"], "CreationDate": str(b["CreationDate"])}
                for b in resp["Buckets"]
            ]
        }
    except Exception as e:
        return {"buckets": [], "error": str(e)}


@app.get("/api/s3/buckets/{bucket}")
def list_objects(bucket: str, prefix: str = ""):
    s3 = oblako.s3.get_client()
    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
        objects = [
            {"Key": o["Key"], "Size": o["Size"], "LastModified": str(o["LastModified"])}
            for o in resp.get("Contents", [])
        ]
        prefixes = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]
        return {"objects": objects, "prefixes": prefixes, "bucket": bucket}
    except Exception as e:
        return {"objects": [], "prefixes": [], "error": str(e)}


# -----------------------------------------------------------------------------------------------
# Step Functions
# -----------------------------------------------------------------------------------------------
@app.get("/api/stepfunctions/state-machines")
def list_state_machines():
    sfn = oblako.stepfunctions.get_client()
    try:
        resp = sfn.list_state_machines()
        machines = [
            {
                "name": sm["name"],
                "stateMachineArn": sm["stateMachineArn"],
                "creationDate": str(sm["creationDate"]),
            }
            for sm in resp["stateMachines"]
        ]
        return {"stateMachines": machines}
    except Exception as e:
        return {"stateMachines": [], "error": str(e)}


@app.get("/api/stepfunctions/describe/{arn:path}")
def describe_state_machine(arn: str):
    sfn = oblako.stepfunctions.get_client()
    try:
        resp = sfn.describe_state_machine(stateMachineArn=arn)
        return {
            "name": resp["name"],
            "stateMachineArn": resp["stateMachineArn"],
            "definition": json.loads(resp["definition"]),
            "status": resp.get("status", "ACTIVE"),
            "creationDate": str(resp["creationDate"]),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/stepfunctions/executions/{arn:path}")
def list_executions(arn: str):
    sfn = oblako.stepfunctions.get_client()
    try:
        resp = sfn.list_executions(stateMachineArn=arn, maxResults=20)
        executions = [
            {
                "executionArn": e["executionArn"],
                "name": e["name"],
                "status": e["status"],
                "startDate": str(e["startDate"]),
            }
            for e in resp["executions"]
        ]
        return {"executions": executions}
    except Exception as e:
        return {"executions": [], "error": str(e)}


@app.get("/api/stepfunctions/templates")
def stepfunctions_templates():
    """List the bundled ML-focused state-machine templates."""
    return {"templates": sfn_templates.public_templates()}


@app.post("/api/stepfunctions/create")
def create_state_machine(body: dict):
    """Create a state machine from a bundled template id, or from a raw name + definition."""
    sfn = oblako.stepfunctions.get_client()
    template_id = body.get("templateId")
    if template_id:
        tpl = sfn_templates.TEMPLATES.get(template_id)
        if not tpl:
            return {"error": f"Unknown template '{template_id}'"}
        name, definition = tpl["name"], tpl["definition"]
        extra = {
            "testCase": tpl["testCase"],
            "runnable": tpl.get("runnable", False),
            "input": tpl["input"],
        }
    else:
        name, definition = body.get("name"), body.get("definition")
        if not name or not definition:
            return {"error": "Provide a templateId, or both name and definition."}
        extra = {"testCase": None, "runnable": False, "input": {}}
    try:
        arn = sfn.create_state_machine(
            name=name,
            definition=json.dumps(definition),
            roleArn=sfn_templates.DUMMY_ROLE,
        )["stateMachineArn"]
    except sfn.exceptions.StateMachineAlreadyExists:
        arn = next(
            m["stateMachineArn"]
            for m in sfn.list_state_machines()["stateMachines"]
            if m["name"] == name
        )
    except Exception as e:
        return {"error": str(e)}
    return {"stateMachineArn": arn, "name": name, **extra}


@app.post("/api/stepfunctions/start")
def start_execution(body: dict):
    """Start an execution; pass a testCase to run in SFN Local mock mode."""
    sfn = oblako.stepfunctions.get_client()
    arn = body.get("stateMachineArn")
    if not arn:
        return {"error": "stateMachineArn is required"}
    test_case = body.get("testCase")
    target = f"{arn}#{test_case}" if test_case else arn
    raw = body.get("input", {})
    payload = raw if isinstance(raw, str) else json.dumps(raw)
    try:
        resp = sfn.start_execution(stateMachineArn=target, input=payload)
        return {"executionArn": resp["executionArn"]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/stepfunctions/execution/{arn:path}")
def describe_execution(arn: str):
    """Return an execution's status, output, and per-state progress."""
    sfn = oblako.stepfunctions.get_client()
    try:
        d = sfn.describe_execution(executionArn=arn)
        steps, seen = [], {}
        for e in sfn.get_execution_history(executionArn=arn)["events"]:
            t = e["type"]
            if t.endswith("StateEntered"):
                name = e["stateEnteredEventDetails"]["name"]
                seen[name] = {
                    "name": name,
                    "type": t[: -len("StateEntered")],
                    "status": "RUNNING",
                }
                steps.append(seen[name])
            elif t.endswith("StateExited"):
                name = e["stateExitedEventDetails"]["name"]
                if name in seen:
                    seen[name]["status"] = "SUCCEEDED"
            elif "Failed" in t:
                for s in reversed(steps):
                    if s["status"] == "RUNNING":
                        s["status"] = "FAILED"
                        break
        return {
            "status": d["status"],
            "output": d.get("output"),
            "error": d.get("error"),
            "cause": d.get("cause"),
            "startDate": str(d.get("startDate", "")),
            "stopDate": str(d.get("stopDate", "")),
            "steps": steps,
        }
    except Exception as e:
        return {"error": str(e)}


# -----------------------------------------------------------------------------------------------
# DynamoDB
# -----------------------------------------------------------------------------------------------
@app.get("/api/dynamodb/tables")
def list_dynamodb_tables():
    try:
        ddb = oblako.dynamodb.get_client()
        resp = ddb.list_tables()
        return {"tables": resp.get("TableNames", [])}
    except Exception as e:
        return {"tables": [], "error": str(e)}


@app.get("/api/dynamodb/tables/{table_name}")
def describe_dynamodb_table(table_name: str):
    try:
        ddb = oblako.dynamodb.get_client()
        desc = ddb.describe_table(TableName=table_name)
        table = desc["Table"]
        # Scan first 50 items
        scan = ddb.scan(TableName=table_name, Limit=50)
        items = []
        for item in scan.get("Items", []):
            row = {}
            for k, v in item.items():
                val = list(v.values())[0]
                row[k] = val
            items.append(row)
        return {
            "table": {
                "TableName": table["TableName"],
                "TableStatus": table["TableStatus"],
                "ItemCount": table["ItemCount"],
                "KeySchema": [
                    {"AttributeName": k["AttributeName"], "KeyType": k["KeyType"]}
                    for k in table["KeySchema"]
                ],
            },
            "items": items,
        }
    except Exception as e:
        return {"table": None, "items": [], "error": str(e)}


# -----------------------------------------------------------------------------------------------
# Redshift
# -----------------------------------------------------------------------------------------------
@app.get("/api/redshift/clusters")
def list_clusters():
    """Redshift control plane (clusters/nodes) via the moto endpoint."""
    try:
        rs = oblako.redshift.get_client()
        resp = rs.describe_clusters()
        clusters = [
            {
                "ClusterIdentifier": c["ClusterIdentifier"],
                "NodeType": c.get("NodeType"),
                "NumberOfNodes": c.get("NumberOfNodes"),
                "ClusterStatus": c.get("ClusterStatus"),
                "Endpoint": c.get("Endpoint", {}),
                "DBName": c.get("DBName"),
            }
            for c in resp.get("Clusters", [])
        ]
        return {"clusters": clusters}
    except Exception as e:
        return {"clusters": [], "error": str(e)}


@app.post("/api/redshift/clusters")
def create_redshift_cluster(body: dict):
    """Create a Redshift cluster via the moto control plane."""
    identifier = body.get("clusterIdentifier")
    if not identifier:
        return {"error": "clusterIdentifier is required"}
    try:
        rs = oblako.redshift.get_client()
        kwargs = {
            "ClusterIdentifier": identifier,
            "NodeType": body.get("nodeType", "ra3.xlplus"),
            "MasterUsername": body.get("masterUsername", "admin"),
            "MasterUserPassword": body.get("masterUserPassword", "Password123"),
            "DBName": body.get("dbName", "dev"),
        }
        nodes = int(body.get("numberOfNodes", 1))
        if nodes > 1:
            kwargs["ClusterType"] = "multi-node"
            kwargs["NumberOfNodes"] = nodes
        else:
            kwargs["ClusterType"] = "single-node"
        resp = rs.create_cluster(**kwargs)
        return {"clusterIdentifier": resp["Cluster"]["ClusterIdentifier"]}
    except Exception as e:
        return {"error": str(e)}


# RDS / Aurora
@app.get("/api/rds/databases")
def list_rds_databases():
    """List RDS instances + Aurora clusters via the moto control plane."""
    try:
        rds = oblako.rds.get_client()
        items = []
        for c in rds.describe_db_clusters().get("DBClusters", []):
            items.append(
                {
                    "id": c["DBClusterIdentifier"],
                    "kind": "cluster",
                    "engine": c.get("Engine"),
                    "status": c.get("Status"),
                    "endpoint": c.get("Endpoint"),
                }
            )
        for i in rds.describe_db_instances().get("DBInstances", []):
            items.append(
                {
                    "id": i["DBInstanceIdentifier"],
                    "kind": "instance",
                    "engine": i.get("Engine"),
                    "status": i.get("DBInstanceStatus"),
                    "endpoint": (i.get("Endpoint") or {}).get("Address"),
                    "instanceClass": i.get("DBInstanceClass"),
                }
            )
        return {"databases": items}
    except Exception as e:
        return {"databases": [], "error": str(e)}


@app.post("/api/rds/databases")
def create_rds_database(body: dict):
    """Create an RDS DB instance or an Aurora cluster via the moto control plane."""
    identifier = body.get("identifier")
    if not identifier:
        return {"error": "identifier is required"}
    engine = body.get("engine", "postgres")  # postgres | mysql
    user = body.get("masterUsername", "admin")
    password = body.get("masterUserPassword", "Password123")
    try:
        rds = oblako.rds.get_client()
        if body.get("mode") == "cluster":  # Aurora
            aurora_engine = (
                "aurora-postgresql" if engine == "postgres" else "aurora-mysql"
            )
            resp = rds.create_db_cluster(
                DBClusterIdentifier=identifier,
                Engine=aurora_engine,
                MasterUsername=user,
                MasterUserPassword=password,
                DatabaseName=body.get("dbName", "app"),
            )
            return {"id": resp["DBCluster"]["DBClusterIdentifier"], "kind": "cluster"}
        resp = rds.create_db_instance(
            DBInstanceIdentifier=identifier,
            Engine=engine,
            DBInstanceClass=body.get("instanceClass", "db.t3.micro"),
            MasterUsername=user,
            MasterUserPassword=password,
            AllocatedStorage=int(body.get("allocatedStorage", 20)),
            DBName=body.get("dbName", "app"),
        )
        return {"id": resp["DBInstance"]["DBInstanceIdentifier"], "kind": "instance"}
    except Exception as e:
        return {"error": str(e)}


# IAM (moto control plane + oblako policy evaluator)
@app.get("/api/iam/overview")
def iam_overview():
    """List IAM users, roles, and customer-managed policies."""
    try:
        iam = oblako.iam.get_client()
        return {
            "users": [
                {"name": u["UserName"], "arn": u["Arn"]}
                for u in iam.list_users()["Users"]
            ],
            "roles": [
                {"name": r["RoleName"], "arn": r["Arn"]}
                for r in iam.list_roles()["Roles"]
            ],
            "policies": [
                {"name": p["PolicyName"], "arn": p["Arn"]}
                for p in iam.list_policies(Scope="Local")["Policies"]
            ],
        }
    except Exception as e:
        return {"users": [], "roles": [], "policies": [], "error": str(e)}


@app.post("/api/iam/users")
def iam_create_user(body: dict):
    """Create an IAM user."""
    try:
        return {"arn": oblako.iam.create_user(body["name"])["Arn"]}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/iam/roles")
def iam_create_role(body: dict):
    """Create an IAM role with a trust policy."""
    try:
        return {"arn": oblako.iam.create_role(body["name"], body["trustPolicy"])["Arn"]}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/iam/policies")
def iam_create_policy(body: dict):
    """Create a customer-managed policy."""
    try:
        return {"arn": oblako.iam.create_policy(body["name"], body["document"])["Arn"]}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/iam/attach")
def iam_attach(body: dict):
    """Attach a managed policy to a role or user."""
    try:
        if body.get("roleName"):
            oblako.iam.attach_role_policy(body["roleName"], body["policyArn"])
        else:
            oblako.iam.attach_user_policy(body["userName"], body["policyArn"])
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/iam/assume-role")
def iam_assume_role(body: dict):
    """Trust-evaluate then sts:AssumeRole as the given principal."""
    try:
        return oblako.iam.assume_role(body["roleArn"], body["principalArn"])
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/iam/simulate")
def iam_simulate(body: dict):
    """Decide whether a principal may perform an action on a resource."""
    try:
        decision = oblako.iam.authorize(
            body["principalArn"], body["action"], body["resource"]
        )
        return {"decision": decision}
    except Exception as e:
        return {"error": str(e)}


# Athena (Trino over the Iceberg catalog)
@app.post("/api/athena/query")
def athena_query(body: dict):
    """Run a SQL query through local Trino; returns {columns, rows} or {error}."""
    sql = (body or {}).get("sql", "").strip()
    if not sql:
        return {"error": "sql is required"}
    try:
        return oblako.trino.query(sql, timeout=120)
    except Exception as e:
        return {"error": {"message": str(e)}}


@app.get("/api/athena/schemas")
def athena_schemas(catalog: str = "iceberg"):
    """Quick schema browser: schemas + their tables under a catalog."""
    try:
        schemas = oblako.trino.query(f"SHOW SCHEMAS FROM {catalog}").get("rows", [])
        result = []
        for (schema,) in schemas:
            if schema in ("information_schema", "system"):
                continue
            tables = oblako.trino.query(f"SHOW TABLES FROM {catalog}.{schema}").get(
                "rows", []
            )
            result.append({"schema": schema, "tables": [t[0] for t in tables]})
        return {"catalog": catalog, "schemas": result}
    except Exception as e:
        return {"catalog": catalog, "schemas": [], "error": str(e)}


# Kinesis
@app.get("/api/kinesis/streams")
def kinesis_streams():
    """List Kinesis streams with status + shard count."""
    try:
        k = oblako.kinesis.get_client()
        names = k.list_streams()["StreamNames"]
        streams = []
        for name in names:
            d = k.describe_stream(StreamName=name)["StreamDescription"]
            streams.append(
                {
                    "name": name,
                    "status": d.get("StreamStatus"),
                    "shards": len(d.get("Shards", [])),
                }
            )
        return {"streams": streams}
    except Exception as e:
        return {"streams": [], "error": str(e)}


@app.post("/api/kinesis/streams")
def kinesis_create_stream(body: dict):
    """Create a Kinesis stream."""
    name = body.get("streamName")
    if not name:
        return {"error": "streamName is required"}
    try:
        oblako.kinesis.get_client().create_stream(
            StreamName=name,
            ShardCount=int(body.get("shardCount", 1)),
        )
        return {"streamName": name}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/kinesis/records")
def kinesis_put_record(body: dict):
    """Put a record onto a Kinesis stream."""
    try:
        resp = oblako.kinesis.get_client().put_record(
            StreamName=body["streamName"],
            Data=body.get("data", "").encode(),
            PartitionKey=body.get("partitionKey", "p1"),
        )
        return {"shardId": resp["ShardId"], "sequenceNumber": resp["SequenceNumber"]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/kinesis/records/{stream_name}")
def kinesis_get_records(stream_name: str, limit: int = 20):
    """Read the most recent records from the stream (TRIM_HORIZON across all shards)."""
    try:
        k = oblako.kinesis.get_client()
        shards = k.describe_stream(StreamName=stream_name)["StreamDescription"][
            "Shards"
        ]
        records = []
        for shard in shards:
            it = k.get_shard_iterator(
                StreamName=stream_name,
                ShardId=shard["ShardId"],
                ShardIteratorType="TRIM_HORIZON",
            )["ShardIterator"]
            page = k.get_records(ShardIterator=it, Limit=limit)["Records"]
            records.extend(
                {
                    "shardId": shard["ShardId"],
                    "partitionKey": r["PartitionKey"],
                    "data": r["Data"].decode("utf-8", errors="replace"),
                    "sequenceNumber": r["SequenceNumber"],
                }
                for r in page
            )
        return {"records": records[-limit:]}
    except Exception as e:
        return {"records": [], "error": str(e)}


@app.get("/api/redshift/schema")
def redshift_schema():
    """Return the schema tree (schemas -> tables -> columns) for the Query Editor browser."""
    try:
        conn = oblako.redshift.connect()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            SELECT table_schema, table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
            ORDER BY table_schema, table_name, ordinal_position
        """)
        schemas: dict[str, dict[str, list]] = {}
        for schema, table, column, dtype in cur.fetchall():
            tables = schemas.setdefault(schema, {})
            tables.setdefault(table, []).append({"name": column, "type": dtype})
        cur.close()
        conn.close()
        return {
            "schemas": [
                {
                    "name": s,
                    "tables": [{"name": t, "columns": cols} for t, cols in ts.items()],
                }
                for s, ts in schemas.items()
            ]
        }
    except Exception as e:
        return {"schemas": [], "error": str(e)}


@app.get("/api/redshift/tables")
def list_tables():
    try:
        conn = oblako.redshift.connect()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' ORDER BY table_name
        """)
        tables = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        return {"tables": tables}
    except Exception as e:
        return {"tables": [], "error": str(e)}


def _decode_field(field: dict):
    """A redshift-data Field union -> a plain Python value."""
    if "isNull" in field:
        return None
    return next(iter(field.values()))


@app.post("/api/redshift/query")
def run_query(body: dict):
    """Run SQL through the Redshift Data API (boto3 'redshift-data')."""
    query = body.get("query", "")
    if not query:
        return {"error": "No query provided"}
    try:
        rd = oblako.redshift.get_data_client()
        stmt_id = rd.execute_statement(Database=oblako.redshift.database, Sql=query)[
            "Id"
        ]
        desc = rd.describe_statement(Id=stmt_id)
        if desc["Status"] == "FAILED":
            return {"error": desc.get("Error", "query failed")}
        if desc.get("HasResultSet"):
            res = rd.get_statement_result(Id=stmt_id)
            columns = [c["name"] for c in res["ColumnMetadata"]]
            rows = [
                dict(zip(columns, [_decode_field(f) for f in record]))
                for record in res["Records"][:100]
            ]
            return {"columns": columns, "rows": rows, "rowCount": res["TotalNumRows"]}
        rows_affected = desc.get("ResultRows", -1)
        if rows_affected is None or rows_affected < 0:
            return {"message": "OK"}
        return {"message": f"OK ({rows_affected} rows affected)"}
    except Exception as e:
        return {"error": str(e)}


# -----------------------------------------------------------------------------------------------
# Bedrock / Ollama
# -----------------------------------------------------------------------------------------------
@app.get("/api/bedrock/models")
def list_models():
    try:
        models = oblako.bedrock.list_models()
        return {"models": [{"modelId": m, "provider": "ollama"} for m in models]}
    except Exception as e:
        return {"models": [], "error": str(e)}


@app.post("/api/bedrock/converse")
def converse(body: dict):
    from oblako.engines.bedrock.adapter import BedrockAdapter

    adapter = BedrockAdapter()
    try:
        return adapter.converse(
            model_id=body.get("modelId", "qwen2.5:0.5b"),
            messages=body.get("messages", []),
            system=body.get("system"),
            inference_config=body.get("inferenceConfig"),
        )
    except Exception as e:
        return {"error": str(e)}


# -----------------------------------------------------------------------------------------------
# SageMaker
# -----------------------------------------------------------------------------------------------
@app.get("/api/sagemaker/containers")
def list_sagemaker_containers():
    try:
        training = oblako.sagemaker.list_training_containers()
        endpoints = oblako.sagemaker.list_endpoint_containers()
        return {"training": training, "endpoints": endpoints}
    except Exception as e:
        return {"training": [], "endpoints": [], "error": str(e)}


@app.get("/api/sagemaker/images")
def list_sagemaker_images():
    try:
        client = oblako.sagemaker.client
        images = client.images.list()
        sagemaker_images = [
            {
                "tags": img.tags,
                "id": img.short_id,
                "size": f"{img.attrs.get('Size', 0) / 1e6:.0f} MB",
            }
            for img in images
            if any(
                "sagemaker" in (t or "").lower()
                or "training" in (t or "").lower()
                or "inference" in (t or "").lower()
                for t in (img.tags or [])
            )
        ]
        return {"images": sagemaker_images}
    except Exception as e:
        return {"images": [], "error": str(e)}


@app.post("/api/sagemaker/cleanup")
def cleanup_sagemaker():
    try:
        removed = oblako.sagemaker.cleanup()
        return {"removed": removed}
    except Exception as e:
        return {"error": str(e)}


# -----------------------------------------------------------------------------------------------
# Notebook (Python execution)
# -----------------------------------------------------------------------------------------------
@app.post("/api/notebook/run")
def run_code(body: dict):
    """Execute a Python snippet against the local oblako stack."""
    import io
    import sys
    import traceback

    code = body.get("code", "")
    if not code.strip():
        return {"error": "No code provided"}

    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    # Pre-inject useful locals so users don't have to import boilerplate
    exec_globals = {
        "__builtins__": __builtins__,
        "oblako": oblako,
        "boto3": __import__("boto3"),
        "json": __import__("json"),
        "pd": None,
    }
    try:
        exec_globals["pd"] = __import__("pandas")
    except ImportError:
        pass

    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout_capture, stderr_capture
    try:
        exec(code, exec_globals)
        output = stdout_capture.getvalue()
        errors = stderr_capture.getvalue()
        return {"output": output, "errors": errors, "status": "ok"}
    except Exception:
        output = stdout_capture.getvalue()
        tb = traceback.format_exc()
        return {"output": output, "errors": tb, "status": "error"}
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr


@app.post("/api/notebook/launch")
def launch_notebook():
    """Spawn JupyterLab (pre-wired to oblako) and return its URL for the UI to open."""
    if importlib.util.find_spec("jupyterlab") is None:
        return {
            "error": "JupyterLab isn't installed. Run: pip install 'oblako[notebook]'"
        }
    try:
        from oblako import notebook

        return notebook.spawn(port=8888)
    except Exception as e:
        return {"error": str(e)}


def _mlflow_urls() -> dict:
    """Vanity + direct URLs for a ready MLflow App."""
    vanity_url, hosts_line = None, None
    try:
        from oblako.services.caddy import vanity_host

        oblako.caddy.start()
        oblako.caddy.wait_ready(timeout=15)
        vanity_url = oblako.caddy.vanity_url(vanity_host("mlflow"))
        hosts_line = oblako.caddy.hosts_line()
    except Exception:
        pass
    return {
        "url": oblako.sagemaker.mlflow.tracking_uri,
        "vanityUrl": vanity_url,
        "hostsLine": hosts_line,
        "arn": oblako.sagemaker.mlflow.tracking_server_arn,
        "customEndpoint": oblako.sagemaker.mlflow.custom_endpoint,
    }


@app.get("/api/mlflow/status")
def mlflow_status():
    """Where the MLflow App stands: idle / starting / ready / error.

    The dashboard uses this to drive an explicit Create + Wait flow (mirrors
    SageMaker's CreateMlflowTrackingServer in real AWS).
    """
    try:
        if oblako.sagemaker.mlflow.wait_ready(timeout=2):
            return {"status": "ready", **_mlflow_urls()}
        return {"status": oblako.sagemaker.mlflow.status().value}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/mlflow/launch")
def launch_mlflow():
    """Create the MLflow App: start the container, return the URLs once ready."""
    try:
        oblako.sagemaker.mlflow.start()
        if not oblako.sagemaker.mlflow.wait_ready(timeout=120):
            return {
                "status": "error",
                "error": "MLflow did not become ready within 120s",
            }
        return {"status": "ready", **_mlflow_urls()}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# SageMaker Studio domain — a CloudFormation stack (S3 artifacts + EC2 notebook
# instance + EBS); the notebook runs JupyterLab inside the instance, pre-wired.
@app.get("/api/sagemaker/domains/{name}/status")
def sagemaker_domain_status(name: str):
    """Domain status (CFN stack state + notebook instance id)."""
    try:
        return oblako.sagemaker.domain_status(name)
    except Exception as e:
        return {"domain": name, "status": "error", "error": str(e)}


@app.post("/api/sagemaker/domains")
def sagemaker_create_domain(body: dict):
    """Create a domain: deploy the CFN stack (S3 + EC2 + EBS). Blocks until done."""
    try:
        return oblako.sagemaker.create_domain(
            body.get("name", "studio"),
            instance_type=body.get("instanceType", "t3.medium"),
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/sagemaker/domains/{name}/notebook")
def sagemaker_launch_notebook(name: str):
    """Launch JupyterLab inside the domain's notebook instance; return its URL."""
    try:
        return {"ok": True, **oblako.sagemaker.launch_notebook(name)}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/sagemaker/domains/{name}")
def sagemaker_delete_domain(name: str):
    """Tear down the domain's CloudFormation stack."""
    try:
        oblako.sagemaker.delete_domain(name)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


# CloudFormation
@app.get("/api/cloudformation/stacks")
def list_stacks():
    cfn = oblako.cloudformation.get_client()
    try:
        resp = cfn.describe_stacks()  # no name -> all stacks
        stacks = [
            {
                "stackName": s["StackName"],
                "stackStatus": s["StackStatus"],
                "creationTime": str(s["CreationTime"]),
                "outputCount": len(s.get("Outputs", [])),
            }
            for s in resp["Stacks"]
        ]
        return {"stacks": stacks}
    except Exception as e:
        return {"stacks": [], "error": str(e)}


@app.get("/api/cloudformation/stacks/{name}")
def describe_stack(name: str):
    cfn = oblako.cloudformation.get_client()
    try:
        stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
        resources = cfn.describe_stack_resources(StackName=name)["StackResources"]
        events = cfn.describe_stack_events(StackName=name)["StackEvents"]
        return {
            "stackName": stack["StackName"],
            "stackStatus": stack["StackStatus"],
            "creationTime": str(stack["CreationTime"]),
            "outputs": [
                {"key": o["OutputKey"], "value": o["OutputValue"]}
                for o in stack.get("Outputs", [])
            ],
            "resources": [
                {
                    "logicalId": r["LogicalResourceId"],
                    "physicalId": r.get("PhysicalResourceId", ""),
                    "type": r["ResourceType"],
                    "status": r.get("ResourceStatus", ""),
                }
                for r in resources
            ],
            "events": [
                {
                    "logicalId": e["LogicalResourceId"],
                    "type": e["ResourceType"],
                    "status": e["ResourceStatus"],
                    "timestamp": str(e["Timestamp"]),
                    "reason": e.get("ResourceStatusReason", ""),
                }
                for e in events
            ],
        }
    except Exception as e:
        return {"error": str(e)}


# Lambda
# Moto owns the state (functions, layers, versions) and — with the Docker socket
# mounted into the moto container — actually executes the handler on invoke.
# moto's GetFunction returns a fake real-AWS S3 URL that we can't fetch back, so
# we cache the source we packed into the zip alongside in a tiny in-memory map.
# Key is (function_name) -> {"filename": str, "source": str}.
_LAMBDA_SOURCE_CACHE: dict[str, dict[str, str]] = {}


def _zip_handler(filename: str, source: str) -> bytes:
    """Pack a single source file into a Lambda-deployable zip."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(filename, source)
    return buf.getvalue()


def _runtime_filename(runtime: str, handler: str) -> str:
    """Pick the source filename inside the zip from the runtime + handler."""
    module = handler.split(".", 1)[0]
    if runtime.startswith("python"):
        return f"{module}.py"
    if runtime.startswith("nodejs"):
        return f"{module}.mjs" if module.endswith(".mjs") else f"{module}.js"
    return f"{module}.py"


def _starter_source(runtime: str, handler: str) -> str:
    """Default source for a freshly created function."""
    fn = handler.rsplit(".", 1)[-1]
    if runtime.startswith("python"):
        return (
            "def " + fn + "(event, context):\n"
            '    return {"statusCode": 200, "body": "hello from oblako", "event": event}\n'
        )
    if runtime.startswith("nodejs"):
        return (
            "export const " + fn + " = async (event) => ({\n"
            "  statusCode: 200, body: 'hello from oblako', event,\n"
            "});\n"
        )
    return "# unsupported runtime — replace this body\n"


@app.get("/api/lambda/functions")
def lambda_list_functions():
    """List Lambda functions registered with moto."""
    lam = oblako.awslambda.get_client()
    try:
        resp = lam.list_functions()
        return {
            "functions": [
                {
                    "name": f["FunctionName"],
                    "runtime": f.get("Runtime", ""),
                    "handler": f.get("Handler", ""),
                    "role": f.get("Role", ""),
                    "memory": f.get("MemorySize", 128),
                    "timeout": f.get("Timeout", 3),
                    "lastModified": f.get("LastModified", ""),
                    "codeSize": f.get("CodeSize", 0),
                    "layers": [lr["Arn"] for lr in f.get("Layers", [])],
                }
                for f in resp.get("Functions", [])
            ]
        }
    except Exception as e:
        return {"error": str(e), "functions": []}


@app.get("/api/lambda/functions/{name}")
def lambda_get_function(name: str):
    """Fetch full function detail incl. handler source (from server-side cache)."""
    lam = oblako.awslambda.get_client()
    try:
        resp = lam.get_function(FunctionName=name)
    except Exception as e:
        return {"error": str(e)}
    cfg = resp["Configuration"]
    cached = _LAMBDA_SOURCE_CACHE.get(name) or {}
    source = cached.get("source")
    source_filename = cached.get("filename")
    return {
        "name": cfg["FunctionName"],
        "runtime": cfg.get("Runtime", ""),
        "handler": cfg.get("Handler", ""),
        "role": cfg.get("Role", ""),
        "memory": cfg.get("MemorySize", 128),
        "timeout": cfg.get("Timeout", 3),
        "description": cfg.get("Description", ""),
        "lastModified": cfg.get("LastModified", ""),
        "codeSize": cfg.get("CodeSize", 0),
        "layers": [lr["Arn"] for lr in cfg.get("Layers", [])],
        "envVars": cfg.get("Environment", {}).get("Variables", {}),
        "source": source,
        "sourceFilename": source_filename,
    }


@app.post("/api/lambda/functions")
def lambda_create_function(body: dict):
    """Create a new function from inline source.

    Defaults Architectures=[x86_64] (real AWS Lambda's default). On Apple Silicon
    the host arch is arm64, so we also pre-pull the x86_64 runtime image so moto
    picks the AWS-default variant when it spawns the function container.
    """
    lam = oblako.awslambda.get_client()
    name = body["name"]
    # Default to python3.12 — its shogo82148 image is on AL2023 (glibc 2.34),
    # matching real-AWS Lambda. python3.11 is still on AL2 (glibc 2.26) and
    # rejects modern pandas/numpy wheels.
    runtime = body.get("runtime", "python3.12")
    handler = body.get("handler", "handler.handler")
    source = body.get("source") or _starter_source(runtime, handler)
    filename = _runtime_filename(runtime, handler)
    role_arn = body.get("role") or oblako.awslambda.ensure_exec_role()
    architecture = body.get("architecture", "x86_64")
    try:
        oblako.awslambda.ensure_runtime_image(runtime, architecture=architecture)
    except Exception:
        pass  # not fatal — moto will fall back to the local image
    try:
        lam.create_function(
            FunctionName=name,
            Runtime=runtime,
            Role=role_arn,
            Handler=handler,
            Code={"ZipFile": _zip_handler(filename, source)},
            Architectures=[architecture],
            Timeout=int(body.get("timeout", 10)),
            MemorySize=int(body.get("memory", 128)),
            Description=body.get("description", ""),
        )
        _LAMBDA_SOURCE_CACHE[name] = {"filename": filename, "source": source}
        return {"ok": True, "name": name}
    except Exception as e:
        return {"error": str(e)}


@app.put("/api/lambda/functions/{name}/code")
def lambda_update_code(name: str, body: dict):
    """Update inline source for an existing function (rezips, calls UpdateFunctionCode)."""
    lam = oblako.awslambda.get_client()
    try:
        cfg = lam.get_function_configuration(FunctionName=name)
        filename = body.get("sourceFilename") or _runtime_filename(
            cfg.get("Runtime", "python3.11"),
            cfg.get("Handler", "handler.handler"),
        )
        lam.update_function_code(
            FunctionName=name,
            ZipFile=_zip_handler(filename, body["source"]),
        )
        _LAMBDA_SOURCE_CACHE[name] = {"filename": filename, "source": body["source"]}
        return {"ok": True, "lastModified": cfg.get("LastModified", "")}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/lambda/functions/{name}")
def lambda_delete_function(name: str):
    """Delete a function."""
    lam = oblako.awslambda.get_client()
    try:
        lam.delete_function(FunctionName=name)
        _LAMBDA_SOURCE_CACHE.pop(name, None)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/lambda/functions/{name}/invoke")
def lambda_invoke(name: str, body: dict):
    """Invoke a function. body = {payload: <event JSON>} — returns the real handler output."""
    import time

    lam = oblako.awslambda.get_client()
    # The UI sends payload as a parsed object; tolerate a raw JSON string too
    # (json.dumps-ing a string would double-encode it, and moto then chokes
    # parsing the invoke body for the qualifier).
    p = body.get("payload")
    if isinstance(p, str):
        payload = p.encode() if p.strip() else b"{}"
    else:
        payload = json.dumps(p or {}).encode()
    started = time.time()
    try:
        r = lam.invoke(FunctionName=name, Payload=payload, LogType="Tail")
    except Exception as e:
        return {"error": str(e)}
    duration_ms = int((time.time() - started) * 1000)
    raw = r["Payload"].read().decode("utf-8", errors="replace")
    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        pass
    log_tail = ""
    if r.get("LogResult"):
        import base64

        try:
            log_tail = base64.b64decode(r["LogResult"]).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            pass
    return {
        "statusCode": r.get("StatusCode"),
        "functionError": r.get("FunctionError"),
        "executedVersion": r.get("ExecutedVersion"),
        "durationMs": duration_ms,
        "payload": parsed if parsed is not None else raw,
        "rawPayload": raw,
        "logTail": log_tail,
    }


@app.get("/api/lambda/layers")
def lambda_list_layers():
    """List Lambda layers (with their latest version)."""
    lam = oblako.awslambda.get_client()
    try:
        resp = lam.list_layers()
        return {
            "layers": [
                {
                    "name": lr["LayerName"],
                    "arn": lr.get("LayerArn", ""),
                    "latestVersion": lr.get("LatestMatchingVersion", {}).get("Version"),
                    "latestVersionArn": lr.get("LatestMatchingVersion", {}).get(
                        "LayerVersionArn", ""
                    ),
                    "runtimes": lr.get("LatestMatchingVersion", {}).get(
                        "CompatibleRuntimes", []
                    ),
                    "description": lr.get("LatestMatchingVersion", {}).get(
                        "Description", ""
                    ),
                }
                for lr in resp.get("Layers", [])
            ]
        }
    except Exception as e:
        return {"error": str(e), "layers": []}


@app.post("/api/lambda/layers")
def lambda_publish_layer(body: dict):
    """Publish a new layer version.

    Two modes:
    - Inline (small): {name, filename, content, runtimes, description} packs a
      single text file into a zip and ships it via Content={ZipFile: ...}.
    - S3 staged (large): {name, s3Bucket, s3Key, runtimes, description} — the
      client first uploads a zip to S3Proxy via POST /api/lambda/layers/s3-upload
      (bypassing this API's request-body limit). We fetch those bytes back here
      and hand them to moto as ZipFile.

      Why not pass Content={S3Bucket, S3Key} straight through? moto's Lambda
      backend resolves an S3 layer reference against *moto's own* in-memory S3
      (port 5500), not S3Proxy (port 9000) — they're separate stores, so moto
      would 404. Bridging the bytes server-side (localhost, no browser limit)
      keeps the big-file upload path working against the real S3 surface.
    """
    lam = oblako.awslambda.get_client()
    name = body["name"]
    # moto derives the layer's /opt mount-volume name by splitting the version
    # ARN on the literal "layer:" and taking the tail. A name ending in "layer"
    # makes "...-layer:<version>" match a second time, so the tail collapses to
    # just the version digit — Docker then rejects it ("volume name too short")
    # at *invoke* time, far from here. Reject it up front with a clear reason.
    if name.endswith("layer"):
        return {
            "error": (
                f"Layer name {name!r} ends in 'layer', which trips a moto volume-naming "
                "bug (the function becomes uninvokable). Pick a name that doesn't end in "
                "'layer' — e.g. 'oblako-pandas' or 'shared-deps'."
            )
        }
    runtimes = body.get("runtimes") or ["python3.11"]
    if body.get("s3Bucket") and body.get("s3Key"):
        s3 = oblako.s3.get_client()
        obj = s3.get_object(Bucket=body["s3Bucket"], Key=body["s3Key"])
        content = {"ZipFile": obj["Body"].read()}
    else:
        filename = body.get("filename", "python/oblako_layer.py")
        text = body.get("content", "# layer content\n")
        content = {"ZipFile": _zip_handler(filename, text)}
    try:
        resp = lam.publish_layer_version(
            LayerName=name,
            Description=body.get("description", ""),
            Content=content,
            CompatibleRuntimes=runtimes,
        )
        return {"ok": True, "version": resp["Version"], "arn": resp["LayerVersionArn"]}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/lambda/layers/s3-upload")
def lambda_layer_s3_upload(body: dict):
    """Issue a presigned PUT URL for staging a large layer zip into S3Proxy.

    Returns {bucket, key, putUrl}. The client uploads the zip to putUrl, then
    POSTs {name, s3Bucket, s3Key, ...} to /api/lambda/layers to publish it.
    """
    import uuid
    from botocore.config import Config
    import boto3

    bucket = body.get("bucket") or "oblako-lambda-layers"
    key = body.get("key") or f"layers/{uuid.uuid4()}.zip"
    s3 = oblako.s3.get_client()
    if bucket not in {b["Name"] for b in s3.list_buckets().get("Buckets", [])}:
        s3.create_bucket(Bucket=bucket)
    signer = boto3.client(
        "s3",
        endpoint_url=oblako.s3.endpoint_url,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name=config.region(),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path", "payload_signing_enabled": False},
        ),
    )
    put_url = signer.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=600,
    )
    return {"bucket": bucket, "key": key, "putUrl": put_url}


@app.post("/api/lambda/functions/{name}/layers")
def lambda_attach_layers(name: str, body: dict):
    """Replace the layer list on a function (Lambda's API is set-not-append)."""
    lam = oblako.awslambda.get_client()
    try:
        lam.update_function_configuration(
            FunctionName=name,
            Layers=body.get("layers", []),
        )
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


# Glue
# Data Catalog: list/create databases + tables (bridged to the Iceberg REST
# catalog). Jobs: submit a PySpark script that runs in amazon/aws-glue-libs:5.
# Workflows are not yet supported in the backend.

# In-memory job history — Glue jobs are per-run containers (no persistent
# tracking), so we keep a tiny log of recent runs here. Keyed by job id.
_GLUE_JOB_HISTORY: list[dict] = []


@app.get("/api/glue/databases")
def glue_list_databases():
    """List Glue Catalog databases (boto3 GetDatabases over the Iceberg bridge)."""
    g = oblako.glue_catalog.get_client()
    try:
        resp = g.get_databases()
        return {
            "databases": [
                {"name": d["Name"], "description": d.get("Description", "")}
                for d in resp.get("DatabaseList", [])
            ]
        }
    except Exception as e:
        return {"error": str(e), "databases": []}


@app.post("/api/glue/databases")
def glue_create_database(body: dict):
    """Create a Glue Catalog database (Iceberg namespace under the hood)."""
    g = oblako.glue_catalog.get_client()
    try:
        g.create_database(
            DatabaseInput={
                "Name": body["name"],
                "Description": body.get("description", ""),
            }
        )
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/glue/databases/{db}/tables")
def glue_list_tables(db: str):
    """List tables in a database (Iceberg tables surfaced through Glue)."""
    g = oblako.glue_catalog.get_client()
    try:
        resp = g.get_tables(DatabaseName=db)
        return {
            "tables": [
                {
                    "name": t["Name"],
                    "tableType": t.get("TableType", ""),
                    "columns": [
                        {"name": c["Name"], "type": c.get("Type", "")}
                        for c in t.get("StorageDescriptor", {}).get("Columns", [])
                    ],
                    "location": t.get("StorageDescriptor", {}).get("Location", ""),
                    "parameters": t.get("Parameters", {}),
                }
                for t in resp.get("TableList", [])
            ]
        }
    except Exception as e:
        return {"error": str(e), "tables": []}


@app.get("/api/glue/databases/{db}/tables/{name}")
def glue_get_table(db: str, name: str):
    """Full table detail incl. Iceberg metadata pointer."""
    g = oblako.glue_catalog.get_client()
    try:
        t = g.get_table(DatabaseName=db, Name=name)["Table"]
        return {
            "name": t["Name"],
            "databaseName": db,
            "tableType": t.get("TableType", ""),
            "columns": [
                {"name": c["Name"], "type": c.get("Type", "")}
                for c in t.get("StorageDescriptor", {}).get("Columns", [])
            ],
            "location": t.get("StorageDescriptor", {}).get("Location", ""),
            "parameters": t.get("Parameters", {}),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/glue/jobs/run")
def glue_run_job(body: dict):
    """Submit a PySpark script to the Glue 5 runner. Synchronous (blocks until done).

    body = {script: str, args?: list[str], env?: dict, timeout?: int}
    Returns {exitCode, logs, durationMs}.
    """
    import time

    script = body.get("script", "").strip()
    if not script:
        return {"error": "Empty script"}
    started = time.time()
    try:
        result = oblako.glue.submit_job(
            script,
            args=body.get("args") or [],
            env=body.get("env") or {},
            timeout=int(body.get("timeout", 600)),
        )
    except Exception as e:
        return {"error": str(e), "durationMs": int((time.time() - started) * 1000)}
    record = {
        "name": body.get("name") or f"job-{int(started)}",
        "exitCode": result["exit_code"],
        "logs": result["logs"][-8000:],
        "durationMs": int((time.time() - started) * 1000),
        "ranAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
    }
    _GLUE_JOB_HISTORY.append(record)
    # Keep history small — these can hold full Spark logs.
    del _GLUE_JOB_HISTORY[:-20]
    return record


@app.get("/api/glue/jobs/history")
def glue_job_history():
    """Most recent Glue job runs (this dashboard process only — not persisted)."""
    return {"jobs": list(reversed(_GLUE_JOB_HISTORY))}


# Workflows: a sequential pipeline of PySpark job steps, success-gated (a step
# runs only if its predecessors succeeded). In-memory run history, like jobs.
_GLUE_WORKFLOW_HISTORY: list[dict] = []


@app.post("/api/glue/workflows/run")
def glue_run_workflow(body: dict):
    """Run a Glue workflow of PySpark steps. Synchronous (blocks until done).

    body = {name?: str, steps: [{name, script}], timeout?: int}
    Returns {name, status, steps:[{name, status, exitCode, logs}], durationMs, ranAt}.
    """
    import time

    steps = body.get("steps") or []
    if not any((s.get("script") or "").strip() for s in steps):
        return {"error": "Provide at least one step with a script."}
    name = body.get("name") or f"workflow-{int(time.time())}"
    started = time.time()
    try:
        result = oblako.glue.run_workflow(
            name,
            steps,
            timeout=int(body.get("timeout", 600)),
        )
    except Exception as e:
        return {"error": str(e), "durationMs": int((time.time() - started) * 1000)}
    record = {
        **result,
        "durationMs": int((time.time() - started) * 1000),
        "ranAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
    }
    _GLUE_WORKFLOW_HISTORY.append(record)
    del _GLUE_WORKFLOW_HISTORY[:-20]
    return record


@app.get("/api/glue/workflows/history")
def glue_workflow_history():
    """Most recent Glue workflow runs (this dashboard process only — not persisted)."""
    return {"workflows": list(reversed(_GLUE_WORKFLOW_HISTORY))}


# EC2
# moto control plane + container-backed instances (instance == container, EBS ==
# Docker volume). Each row shows both the moto state and the live container state.
@app.get("/api/ec2/instances")
def ec2_list_instances():
    """List EC2 instances (moto metadata) with their backing-container status."""
    ec2 = oblako.ec2
    try:
        reservations = ec2.get_client().describe_instances().get("Reservations", [])
    except Exception as e:
        return {"instances": [], "error": str(e)}
    instances = []
    for r in reservations:
        for inst in r.get("Instances", []):
            if inst.get("State", {}).get("Name") == "terminated":
                continue
            iid = inst["InstanceId"]
            container = ec2.instance_container(iid)
            name = next(
                (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), ""
            )
            instances.append(
                {
                    "id": iid,
                    "name": name,
                    "type": inst.get("InstanceType", ""),
                    "state": inst.get("State", {}).get("Name", ""),
                    "imageId": inst.get("ImageId", ""),
                    "containerStatus": container.status if container else "—",
                }
            )
    return {"instances": instances}


@app.post("/api/ec2/instances")
def ec2_run_instance(body: dict):
    """Launch a container-backed instance. body = {instanceType?, name?, backed?}."""
    ec2 = oblako.ec2
    try:
        iid = ec2.run_instance(
            instance_type=body.get("instanceType", "t3.micro"),
            backed=body.get("backed", True),
        )
        if body.get("name"):
            ec2.get_client().create_tags(
                Resources=[iid],
                Tags=[{"Key": "Name", "Value": body["name"]}],
            )
        return {"ok": True, "id": iid}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/ec2/instances/{iid}/stop")
def ec2_stop_instance(iid: str):
    """Stop an instance (container stops; EBS volume kept)."""
    try:
        oblako.ec2.stop_instance(iid)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/ec2/instances/{iid}/start")
def ec2_start_instance(iid: str):
    """Start a stopped instance (container starts)."""
    try:
        oblako.ec2.start_instance(iid)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/ec2/instances/{iid}")
def ec2_terminate_instance(iid: str):
    """Terminate an instance (container + EBS volume removed)."""
    try:
        oblako.ec2.terminate_instance(iid)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


# AppConfig
def _ac():
    return oblako.appconfig.get_client()


@app.get("/api/appconfig/applications")
def appconfig_applications():
    """List AppConfig applications, each with its environment + profile counts."""
    try:
        ac = _ac()
        apps = []
        for a in ac.list_applications().get("Items", []):
            envs = ac.list_environments(ApplicationId=a["Id"]).get("Items", [])
            profs = ac.list_configuration_profiles(ApplicationId=a["Id"]).get(
                "Items", []
            )
            apps.append(
                {
                    "id": a["Id"],
                    "name": a["Name"],
                    "description": a.get("Description", ""),
                    "environments": len(envs),
                    "profiles": len(profs),
                }
            )
        return {"applications": apps}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/appconfig/applications")
def appconfig_create_application(body: dict):
    """Create an AppConfig application."""
    try:
        a = _ac().create_application(
            Name=body["name"], Description=body.get("description", "")
        )
        return {"id": a["Id"], "name": a["Name"]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/appconfig/applications/{app_id}/environments")
def appconfig_environments(app_id: str):
    """List an application's environments."""
    try:
        return {
            "environments": [
                {"id": e["Id"], "name": e["Name"], "state": e.get("State", "")}
                for e in _ac().list_environments(ApplicationId=app_id).get("Items", [])
            ]
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/appconfig/applications/{app_id}/environments")
def appconfig_create_environment(app_id: str, body: dict):
    """Create an environment under an application."""
    try:
        e = _ac().create_environment(
            ApplicationId=app_id,
            Name=body["name"],
            Description=body.get("description", ""),
        )
        return {"id": e["Id"], "name": e["Name"]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/appconfig/applications/{app_id}/profiles")
def appconfig_profiles(app_id: str):
    """List an application's configuration profiles."""
    try:
        return {
            "profiles": [
                {
                    "id": p["Id"],
                    "name": p["Name"],
                    "type": p.get("Type", "AWS.Freeform"),
                    "locationUri": p.get("LocationUri", "hosted"),
                }
                for p in _ac()
                .list_configuration_profiles(ApplicationId=app_id)
                .get("Items", [])
            ]
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/appconfig/applications/{app_id}/profiles")
def appconfig_create_profile(app_id: str, body: dict):
    """Create a configuration profile (Freeform or FeatureFlags)."""
    try:
        p = _ac().create_configuration_profile(
            ApplicationId=app_id,
            Name=body["name"],
            LocationUri=body.get("locationUri", "hosted"),
            Type=body.get("type", "AWS.Freeform"),
        )
        return {"id": p["Id"], "name": p["Name"]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/appconfig/applications/{app_id}/profiles/{profile_id}/versions")
def appconfig_versions(app_id: str, profile_id: str):
    """List hosted configuration versions for a profile (newest first)."""
    try:
        items = (
            _ac()
            .list_hosted_configuration_versions(
                ApplicationId=app_id, ConfigurationProfileId=profile_id
            )
            .get("Items", [])
        )
        return {
            "versions": [
                {
                    "versionNumber": v["VersionNumber"],
                    "contentType": v.get("ContentType", ""),
                    "description": v.get("Description", ""),
                }
                for v in items
            ]
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/appconfig/applications/{app_id}/profiles/{profile_id}/versions/{number}")
def appconfig_version_content(app_id: str, profile_id: str, number: int):
    """Return one hosted version's raw content (decoded as text)."""
    try:
        v = _ac().get_hosted_configuration_version(
            ApplicationId=app_id,
            ConfigurationProfileId=profile_id,
            VersionNumber=number,
        )
        return {
            "versionNumber": v["VersionNumber"],
            "content": v["Content"].read().decode("utf-8", "replace"),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/appconfig/applications/{app_id}/profiles/{profile_id}/versions")
def appconfig_create_version(app_id: str, profile_id: str, body: dict):
    """Store a new hosted configuration version from JSON/text content."""
    try:
        content = body.get("content", "")
        # Validate JSON early so the UI gets a clear error, not a wire failure.
        json.loads(content)
        v = _ac().create_hosted_configuration_version(
            ApplicationId=app_id,
            ConfigurationProfileId=profile_id,
            Content=content.encode(),
            ContentType=body.get("contentType", "application/json"),
        )
        return {"versionNumber": v["VersionNumber"]}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/appconfig/strategies")
def appconfig_strategies():
    """List deployment strategies (predefined + custom)."""
    try:
        return {
            "strategies": [
                {
                    "id": s["Id"],
                    "name": s["Name"],
                    "duration": s.get("DeploymentDurationInMinutes", 0),
                    "growthFactor": s.get("GrowthFactor", 100.0),
                }
                for s in _ac().list_deployment_strategies().get("Items", [])
            ]
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/appconfig/applications/{app_id}/environments/{env_id}/deployments")
def appconfig_deployments(app_id: str, env_id: str):
    """List an environment's deployments (newest first)."""
    try:
        items = (
            _ac()
            .list_deployments(ApplicationId=app_id, EnvironmentId=env_id)
            .get("Items", [])
        )
        return {
            "deployments": [
                {
                    "deploymentNumber": d["DeploymentNumber"],
                    "profileId": d.get("ConfigurationProfileId", ""),
                    "version": d.get("ConfigurationVersion", ""),
                    "strategyId": d.get("DeploymentStrategyId", ""),
                    "state": d.get("State", ""),
                    "percentageComplete": d.get("PercentageComplete", 0),
                }
                for d in items
            ]
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/appconfig/applications/{app_id}/environments/{env_id}/deployments")
def appconfig_start_deployment(app_id: str, env_id: str, body: dict):
    """Start a deployment of a profile version to an environment."""
    try:
        d = _ac().start_deployment(
            ApplicationId=app_id,
            EnvironmentId=env_id,
            ConfigurationProfileId=body["profileId"],
            ConfigurationVersion=str(body["version"]),
            DeploymentStrategyId=body.get("strategyId", "AppConfig.AllAtOnce"),
        )
        return {"deploymentNumber": d["DeploymentNumber"], "state": d.get("State", "")}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/appconfig/evaluate")
def appconfig_evaluate(body: dict):
    """Resolve feature-flag variants for a profile's latest version against a context.

    This is the agent's job: fetch the raw config, then run the rule evaluator
    (eq/and/or/split/…) over its ``values`` map. Returns the resolved flags so the
    UI can show which variant each context lands in (incl. the A/B ``split``).
    """
    try:
        from oblako.engines.appconfig import evaluate_config

        ac = _ac()
        v = ac.get_hosted_configuration_version(
            ApplicationId=body["applicationId"],
            ConfigurationProfileId=body["profileId"],
            VersionNumber=int(body["version"]),
        )
        raw = json.loads(v["Content"].read())
        values = raw.get("values", raw) if isinstance(raw, dict) else {}
        context = body.get("context") or {}
        return {"flags": evaluate_config(values, context)}
    except json.JSONDecodeError as e:
        return {"error": f"Configuration is not valid JSON: {e}"}
    except Exception as e:
        return {"error": str(e)}


# Static frontend (production build)
if DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

    # index.html must never be cached: it references the hashed JS bundle, so a
    # cached copy pins the browser to a stale build after a rebuild. The hashed
    # assets under /assets are immutable (name changes per build) and cache freely.
    _NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    @app.get("/{path:path}")
    def serve_frontend(path: str):
        file = DIST_DIR / path
        if file.exists() and file.is_file() and file.name != "index.html":
            return FileResponse(file)
        return FileResponse(DIST_DIR / "index.html", headers=_NO_CACHE)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
