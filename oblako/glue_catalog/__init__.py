"""boto3-compatible Glue Data Catalog over the Iceberg REST catalog.

AWS Glue's catalog API (``GetDatabases`` / ``GetTable`` / …) speaks AWS json-1.1
on a single ``POST /`` with ``X-Amz-Target: AWSGlue.<Action>``. This module
serves that wire protocol locally and translates the calls to oblako's Iceberg
REST catalog (= S3 Tables). Result: ``boto3.client("glue", endpoint_url=…)``
reads/writes the same catalog Spark/Trino/pyiceberg see.

Started in-process via :func:`start_in_thread` like the other oblako-authored
servers (cloudformation, bedrock_runtime, lambda_shim).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

DEFAULT_PORT = 8486
DEFAULT_ICEBERG_URL = "http://localhost:8181"

_lock = threading.Lock()
_servers: dict[int, object] = {}

# Maps Glue actions (X-Amz-Target value) to translator functions.
_ACTIONS: dict[str, callable] = {}


def _action(name: str):
    def deco(fn):
        _ACTIONS[name] = fn
        return fn

    return deco


def _iceberg_url() -> str:
    return os.environ.get("OBLAKO_ICEBERG_URL") or DEFAULT_ICEBERG_URL


def _iceberg_to_glue_type(t) -> str:
    """Map an Iceberg field type (str or {type: 'struct', ...}) to a Glue/Hive type string."""
    if isinstance(t, dict):
        return "string"  # nested struct/list/map -> coarse fallback
    return {
        "int": "int", "long": "bigint", "float": "float", "double": "double",
        "string": "string", "boolean": "boolean", "date": "date",
        "timestamp": "timestamp", "timestamptz": "timestamp", "uuid": "string",
        "binary": "binary",
    }.get(t, "string")


def _table_to_glue(database: str, name: str, table_json: dict) -> dict:
    schemas = (table_json.get("metadata") or {}).get("schemas") or []
    fields = (schemas[-1] if schemas else {}).get("fields") or []
    return {
        "Name": name,
        "DatabaseName": database,
        "TableType": "EXTERNAL_TABLE",
        "StorageDescriptor": {
            "Columns": [{"Name": f.get("name"), "Type": _iceberg_to_glue_type(f.get("type"))}
                        for f in fields],
            "Location": table_json.get("metadata-location", ""),
        },
        "Parameters": {"table_type": "ICEBERG"},
    }


@_action("AWSGlue.GetDatabases")
def _get_databases(body):
    resp = httpx.get(f"{_iceberg_url()}/v1/namespaces", timeout=5.0)
    namespaces = resp.json().get("namespaces", [])
    return {"DatabaseList": [{"Name": ".".join(n)} for n in namespaces]}


@_action("AWSGlue.GetDatabase")
def _get_database(body):
    name = body["Name"]
    resp = httpx.get(f"{_iceberg_url()}/v1/namespaces/{name}", timeout=5.0)
    if resp.status_code != 200:
        return None
    return {"Database": {"Name": name}}


@_action("AWSGlue.CreateDatabase")
def _create_database(body):
    name = body["DatabaseInput"]["Name"]
    httpx.post(f"{_iceberg_url()}/v1/namespaces",
               json={"namespace": [name]}, timeout=5.0)
    return {}


@_action("AWSGlue.GetTables")
def _get_tables(body):
    db = body["DatabaseName"]
    ids = httpx.get(f"{_iceberg_url()}/v1/namespaces/{db}/tables",
                    timeout=5.0).json().get("identifiers", [])
    tables = []
    for ident in ids:
        name = ident["name"]
        tjson = httpx.get(f"{_iceberg_url()}/v1/namespaces/{db}/tables/{name}",
                          timeout=5.0).json()
        tables.append(_table_to_glue(db, name, tjson))
    return {"TableList": tables}


@_action("AWSGlue.GetTable")
def _get_table(body):
    db, name = body["DatabaseName"], body["Name"]
    resp = httpx.get(f"{_iceberg_url()}/v1/namespaces/{db}/tables/{name}", timeout=5.0)
    if resp.status_code != 200:
        return None
    return {"Table": _table_to_glue(db, name, resp.json())}


async def _glue_dispatch(request: Request) -> JSONResponse:
    target = request.headers.get("X-Amz-Target", "")
    handler = _ACTIONS.get(target)
    if handler is None:
        return JSONResponse(
            {"__type": "InvalidAction", "Message": f"Unsupported action: {target}"},
            status_code=400,
            headers={"x-amzn-errortype": "InvalidAction"},
        )
    body = json.loads(await request.body() or b"{}")
    try:
        result = handler(body)
    except Exception as e:  # noqa: BLE001 - surface as Glue InternalFailure
        return JSONResponse(
            {"__type": "InternalFailure", "Message": str(e)},
            status_code=500, headers={"x-amzn-errortype": "InternalFailure"},
        )
    if result is None:
        return JSONResponse(
            {"__type": "EntityNotFoundException", "Message": "Not found"},
            status_code=400, headers={"x-amzn-errortype": "EntityNotFoundException"},
        )
    return JSONResponse(result)


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "actions": sorted(_ACTIONS)})


def create_app() -> Starlette:
    """Build the Starlette ASGI app implementing the Glue Data Catalog wire protocol."""
    return Starlette(routes=[
        Route("/", _health, methods=["GET"]),
        Route("/", _glue_dispatch, methods=["POST"]),
    ])


app = create_app()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if a glue_catalog server is reachable on the port."""
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def start_in_thread(port: int = DEFAULT_PORT) -> str:
    """Start the Glue-API shim in a daemon thread (idempotent)."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        ucfg = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(ucfg)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        _servers[port] = server

    deadline = time.time() + 10
    while time.time() < deadline:
        if is_running(port):
            return url
        time.sleep(0.1)
    raise RuntimeError(f"glue_catalog server did not start on port {port}")
