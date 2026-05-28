"""oblako dashboard API: exposes local services to the Cloudscape frontend."""

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
DIST_DIR = Path(__file__).parent / "ui" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bring up the Lambda shim so Step Functions lambda:invoke tasks (e.g. the
    # Bedrock prompt-chain -> local model) can run live against oblako services.
    try:
        from oblako import lambda_shim

        lambda_shim.start_in_thread()
    except Exception:  # noqa: BLE001 - dashboard still works without live SFN runs
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
    return {"region": config.region(), "accountId": config.account_id(), "regions": config.REGIONS}


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
        extra = {"testCase": tpl["testCase"], "runnable": tpl.get("runnable", False), "input": tpl["input"]}
    else:
        name, definition = body.get("name"), body.get("definition")
        if not name or not definition:
            return {"error": "Provide a templateId, or both name and definition."}
        extra = {"testCase": None, "runnable": False, "input": {}}
    try:
        arn = sfn.create_state_machine(name=name, definition=json.dumps(definition),
                                       roleArn=sfn_templates.DUMMY_ROLE)["stateMachineArn"]
    except sfn.exceptions.StateMachineAlreadyExists:
        arn = next(m["stateMachineArn"] for m in sfn.list_state_machines()["stateMachines"]
                   if m["name"] == name)
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
                seen[name] = {"name": name, "type": t[: -len("StateEntered")], "status": "RUNNING"}
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
            items.append({
                "id": c["DBClusterIdentifier"], "kind": "cluster",
                "engine": c.get("Engine"), "status": c.get("Status"),
                "endpoint": c.get("Endpoint"),
            })
        for i in rds.describe_db_instances().get("DBInstances", []):
            items.append({
                "id": i["DBInstanceIdentifier"], "kind": "instance",
                "engine": i.get("Engine"), "status": i.get("DBInstanceStatus"),
                "endpoint": (i.get("Endpoint") or {}).get("Address"),
                "instanceClass": i.get("DBInstanceClass"),
            })
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
            aurora_engine = "aurora-postgresql" if engine == "postgres" else "aurora-mysql"
            resp = rds.create_db_cluster(
                DBClusterIdentifier=identifier, Engine=aurora_engine,
                MasterUsername=user, MasterUserPassword=password,
                DatabaseName=body.get("dbName", "app"),
            )
            return {"id": resp["DBCluster"]["DBClusterIdentifier"], "kind": "cluster"}
        resp = rds.create_db_instance(
            DBInstanceIdentifier=identifier, Engine=engine,
            DBInstanceClass=body.get("instanceClass", "db.t3.micro"),
            MasterUsername=user, MasterUserPassword=password,
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
            "users": [{"name": u["UserName"], "arn": u["Arn"]} for u in iam.list_users()["Users"]],
            "roles": [{"name": r["RoleName"], "arn": r["Arn"]} for r in iam.list_roles()["Roles"]],
            "policies": [{"name": p["PolicyName"], "arn": p["Arn"]}
                         for p in iam.list_policies(Scope="Local")["Policies"]],
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
        decision = oblako.iam.authorize(body["principalArn"], body["action"], body["resource"])
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
    except Exception as e:  # noqa: BLE001
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
            tables = oblako.trino.query(f"SHOW TABLES FROM {catalog}.{schema}").get("rows", [])
            result.append({"schema": schema, "tables": [t[0] for t in tables]})
        return {"catalog": catalog, "schemas": result}
    except Exception as e:  # noqa: BLE001
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
            streams.append({"name": name, "status": d.get("StreamStatus"),
                            "shards": len(d.get("Shards", []))})
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
            StreamName=name, ShardCount=int(body.get("shardCount", 1)),
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
        shards = k.describe_stream(StreamName=stream_name)["StreamDescription"]["Shards"]
        records = []
        for shard in shards:
            it = k.get_shard_iterator(StreamName=stream_name, ShardId=shard["ShardId"],
                                      ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
            page = k.get_records(ShardIterator=it, Limit=limit)["Records"]
            records.extend({
                "shardId": shard["ShardId"],
                "partitionKey": r["PartitionKey"],
                "data": r["Data"].decode("utf-8", errors="replace"),
                "sequenceNumber": r["SequenceNumber"],
            } for r in page)
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
        cur.close(); conn.close()
        return {"schemas": [{"name": s, "tables": [{"name": t, "columns": cols}
                             for t, cols in ts.items()]} for s, ts in schemas.items()]}
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
        stmt_id = rd.execute_statement(Database=oblako.redshift.database, Sql=query)["Id"]
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
    from oblako.bedrock.adapter import BedrockAdapter

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
    try:
        import jupyterlab  # noqa: F401
    except ImportError:
        return {"error": "JupyterLab isn't installed. Run: pip install 'oblako[notebook]'"}
    try:
        from oblako import notebook
        return notebook.spawn(port=8888)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@app.post("/api/mlflow/launch")
def launch_mlflow():
    """Start MLflow + the Caddy vanity proxy; return both the direct and AWS-shaped URLs."""
    try:
        oblako.mlflow.start()
        if not oblako.mlflow.wait_ready(timeout=60):
            return {"error": "MLflow did not become ready within 60s"}
        # Caddy is best-effort — if :80 is busy or it errors, fall back to localhost.
        vanity_url, hosts_line = None, None
        try:
            from oblako.services.caddy import vanity_host

            oblako.caddy.start()
            oblako.caddy.wait_ready(timeout=15)
            vanity_url = oblako.caddy.vanity_url(vanity_host("mlflow"))
            hosts_line = oblako.caddy.hosts_line()
        except Exception:  # noqa: BLE001
            pass
        return {
            "url": oblako.mlflow.tracking_uri,
            "vanityUrl": vanity_url,
            "hostsLine": hosts_line,
        }
    except Exception as e:  # noqa: BLE001
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
            "outputs": [{"key": o["OutputKey"], "value": o["OutputValue"]} for o in stack.get("Outputs", [])],
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


# -----------------------------------------------------------------------------------------------
# Static frontend (production build)
# -----------------------------------------------------------------------------------------------
if DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

    @app.get("/{path:path}")
    def serve_frontend(path: str):
        file = DIST_DIR / path
        if file.exists() and file.is_file():
            return FileResponse(file)
        return FileResponse(DIST_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
