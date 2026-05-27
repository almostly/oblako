"""oblako dashboard API: exposes local services to the Cloudscape frontend."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from oblako.services.platform import Oblako

oblako = Oblako()
DIST_DIR = Path(__file__).parent / "ui" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
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
