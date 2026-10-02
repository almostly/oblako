"""boto3-compatible Glue Data Catalog: Iceberg tables and Hive-style tables.

AWS Glue's catalog API (``GetDatabases`` / ``CreateTable`` / …) speaks AWS
json-1.1 on a single ``POST /`` with ``X-Amz-Target: AWSGlue.<Action>``. This
module serves that wire protocol locally over two stores:

- Iceberg tables live in oblako's Iceberg REST catalog (= S3 Tables), so Spark,
  Trino, PyIceberg and the ``glue`` client all see one catalog. A table created
  through Glue with ``table_type=ICEBERG`` and a ``metadata_location`` (what
  PyIceberg's Glue catalog sends) is registered there, and ``UpdateTable`` moves
  it to the new metadata file, checked against ``previous_metadata_location``.
- Every other table (Parquet / CSV / JSON under an S3 location, as awswrangler
  and Athena CTAS create them) and its partitions are kept in SQLite
  (:mod:`.store`).

A database is both a REST namespace and its Glue metadata. Trino's Hive
connector uses this engine as its Glue metastore (the ``awsdatacatalog``
catalog Athena queries), so it listens on all interfaces for the container.

Started in-process via :func:`start_in_thread` like the other oblako-authored
servers (cloudformation, bedrock_runtime, lambda_shim).
"""

from __future__ import annotations

import importlib
import json
import os
import re
import threading
import time
from collections.abc import Callable

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from oblako import config
from oblako.engines.glue_catalog.expression import compile_expression
from oblako.engines.glue_catalog.store import GlueStore
from oblako.engines.identity import claim_port, identify, is_engine

DEFAULT_PORT = 8486
DEFAULT_ICEBERG_URL = "http://localhost:8181"

_lock = threading.Lock()
_servers: dict[int, object] = {}
_stores: list[GlueStore] = []

# Maps Glue actions (X-Amz-Target value) to translator functions.
_ACTIONS: dict[str, Callable[[dict], dict]] = {}


class GlueError(Exception):
    """A Glue error response (``__type`` + message, HTTP 400)."""

    def __init__(self, code: str, message: str):
        """Keep the Glue error code with the message."""
        super().__init__(message)
        self.code = code


def _not_found(message: str) -> GlueError:
    return GlueError("EntityNotFoundException", message)


def _action(name: str):
    def deco(fn):
        _ACTIONS[name] = fn
        return fn

    return deco


def _store() -> GlueStore:
    if not _stores:
        with _lock:
            if not _stores:
                _stores.append(GlueStore())
    return _stores[0]


def _iceberg_url() -> str:
    return os.environ.get("OBLAKO_ICEBERG_URL") or DEFAULT_ICEBERG_URL


# ---------------------------------------------------------------------------
# Iceberg REST catalog
# ---------------------------------------------------------------------------
def _rest(method: str, path: str, **kwargs) -> httpx.Response | None:
    """Call the Iceberg REST catalog; None if it isn't running."""
    try:
        return httpx.request(
            method, f"{_iceberg_url()}/v1{path}", timeout=10.0, **kwargs
        )
    except httpx.TransportError:
        return None


def _rest_namespaces() -> list[str]:
    resp = _rest("GET", "/namespaces")
    if resp is None or resp.status_code != 200:
        return []
    return [".".join(n) for n in resp.json().get("namespaces", [])]


def _rest_namespace(name: str) -> dict | None:
    resp = _rest("GET", f"/namespaces/{name}")
    return resp.json() if resp is not None and resp.status_code == 200 else None


def _rest_tables(db: str) -> list[str]:
    resp = _rest("GET", f"/namespaces/{db}/tables")
    if resp is None or resp.status_code != 200:
        return []
    return [i["name"] for i in resp.json().get("identifiers", [])]


def _register(db: str, name: str, metadata_location: str) -> None:
    """Register an Iceberg metadata file in the REST catalog as db.name."""
    if _rest_namespace(db) is None:
        _rest("POST", "/namespaces", json={"namespace": [db]})
    resp = _rest(
        "POST",
        f"/namespaces/{db}/register",
        json={"name": name, "metadata-location": metadata_location},
    )
    if resp is None or resp.status_code >= 300:
        detail = "unreachable" if resp is None else resp.text[:300]
        raise GlueError(
            "InternalServiceException",
            f"registering {db}.{name} in the Iceberg REST catalog failed: {detail}",
        )


def _drop_registration(db: str, name: str) -> None:
    """Drop an Iceberg table from the REST catalog, keeping its files (as Glue does)."""
    _rest(
        "DELETE",
        f"/namespaces/{db}/tables/{name}",
        params={"purgeRequested": "false"},
    )


def _iceberg_to_glue_type(t) -> str:
    """Map an Iceberg field type (str or {type: 'struct', ...}) to a Glue/Hive type string."""
    if isinstance(t, dict):
        return "string"  # nested struct/list/map -> coarse fallback
    return {
        "int": "int",
        "long": "bigint",
        "float": "float",
        "double": "double",
        "string": "string",
        "boolean": "boolean",
        "date": "date",
        "timestamp": "timestamp",
        "timestamptz": "timestamp",
        "uuid": "string",
        "binary": "binary",
    }.get(t, "string")


def _table_to_glue(
    database: str, name: str, table_json: dict, stored: dict | None = None
) -> dict:
    """Describe a REST-catalog Iceberg table as Glue does.

    ``stored`` is the TableInput it was created or last updated with through
    Glue, if any; its column types, comments and parameters are kept, while
    the columns themselves and ``metadata_location`` follow the REST catalog
    (Trino or Spark may have committed since).
    """
    metadata = table_json.get("metadata") or {}
    schemas = metadata.get("schemas") or []
    schema = next(
        (s for s in schemas if s.get("schema-id") == metadata.get("current-schema-id")),
        schemas[-1] if schemas else {},
    )
    metadata_location = table_json.get("metadata-location", "")
    location = metadata.get("location") or metadata_location.rsplit("/metadata/", 1)[0]
    table = dict(stored or {})
    sd = dict(table.get("StorageDescriptor") or {})
    known = {c["Name"]: c for c in sd.get("Columns") or []}
    sd["Columns"] = [
        {
            "Name": f.get("name"),
            "Type": _iceberg_to_glue_type(f.get("type")),
            **known.get(f.get("name"), {}),
        }
        for f in schema.get("fields") or []
    ]
    sd["Location"] = location
    table.update(
        Name=name,
        DatabaseName=database,
        TableType=table.get("TableType") or "EXTERNAL_TABLE",
        StorageDescriptor=sd,
        Parameters={
            **(table.get("Parameters") or {}),
            "table_type": "ICEBERG",
            "metadata_location": metadata_location,
        },
    )
    table.setdefault("VersionId", "0")
    table.setdefault("CatalogId", config.account_id())
    return table


def _is_iceberg(table: dict) -> bool:
    params = table.get("Parameters") or {}
    return str(params.get("table_type", "")).upper() == "ICEBERG"


def _page(items: list, body: dict, default_size: int) -> tuple[list, dict]:
    """Slice one page and the NextToken that continues it."""
    start = int(body.get("NextToken") or 0)
    size = int(body.get("MaxResults") or default_size)
    more = {"NextToken": str(start + size)} if start + size < len(items) else {}
    return items[start : start + size], more


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------
def _database(name: str) -> dict | None:
    stored = _store().database(name)
    namespace = _rest_namespace(name)
    if stored is None and namespace is None:
        return None
    db = {"Name": name, "CatalogId": config.account_id()}
    location = ((namespace or {}).get("properties") or {}).get("location")
    if location:
        db["LocationUri"] = location
    if stored:
        db.update({k: v for k, v in stored.items() if k != "Name"})
    return db


def _require_database(name: str) -> dict:
    db = _database(name)
    if db is None:
        raise _not_found(f"Database {name} not found.")
    return db


@_action("AWSGlue.GetDatabases")
def _get_databases(body):
    names = sorted(set(_store().databases()) | set(_rest_namespaces()))
    page, more = _page(names, body, 100)
    return {"DatabaseList": [d for d in map(_database, page) if d], **more}


@_action("AWSGlue.GetDatabase")
def _get_database(body):
    return {"Database": _require_database(body["Name"].lower())}


@_action("AWSGlue.CreateDatabase")
def _create_database(body):
    db_input = dict(body["DatabaseInput"])
    name = db_input["Name"] = db_input["Name"].lower()
    if _database(name) is not None:
        raise GlueError("AlreadyExistsException", f"Database {name} already exists.")
    properties = (
        {"location": db_input["LocationUri"]} if db_input.get("LocationUri") else {}
    )
    _rest("POST", "/namespaces", json={"namespace": [name], "properties": properties})
    _store().put_database(name, db_input)
    return {}


@_action("AWSGlue.UpdateDatabase")
def _update_database(body):
    name = body["Name"].lower()
    _require_database(name)
    _store().put_database(name, {**body["DatabaseInput"], "Name": name})
    return {}


@_action("AWSGlue.DeleteDatabase")
def _delete_database(body):
    name = body["Name"].lower()
    _require_database(name)
    for table in _rest_tables(name):
        _drop_registration(name, table)
    _rest("DELETE", f"/namespaces/{name}")
    _store().delete_database(name)
    return {}


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
def _table(db: str, name: str) -> dict | None:
    """Return a table as Glue describes it, from whichever store holds it."""
    name = name.lower()
    stored = _store().table(db, name)
    if stored is not None and not _is_iceberg(stored):
        return {**stored, "CatalogId": config.account_id()}
    resp = _rest("GET", f"/namespaces/{db}/tables/{name}")
    if resp is None:
        if stored is not None:
            raise GlueError(
                "InternalServiceException",
                "the Iceberg REST catalog isn't reachable",
            )
        return None
    if resp.status_code != 200:
        if stored is not None:  # dropped through the REST catalog since
            _store().delete_table(db, name)
        return None
    return _table_to_glue(db, name, resp.json(), stored)


def _require_table(db: str, name: str) -> dict:
    table = _table(db, name)
    if table is None:
        raise _not_found(f"Table {name} not found.")
    return table


def _name_matches(pattern: str, name: str) -> bool:
    """Glue's table-name Expression: a regex, where ``*`` alone means any text."""
    regex = re.sub(r"(?<!\.)\*", ".*", pattern)
    try:
        return re.fullmatch(regex, name, re.IGNORECASE) is not None
    except re.error:
        return pattern.strip("*").lower() in name


@_action("AWSGlue.GetTables")
def _get_tables(body):
    db = body["DatabaseName"].lower()
    _require_database(db)
    names = sorted({t["Name"] for t in _store().tables(db)} | set(_rest_tables(db)))
    if body.get("Expression"):
        names = [n for n in names if _name_matches(body["Expression"], n)]
    page, more = _page(names, body, 100)
    return {"TableList": [t for t in (_table(db, n) for n in page) if t], **more}


@_action("AWSGlue.GetTable")
def _get_table(body):
    return {"Table": _require_table(body["DatabaseName"].lower(), body["Name"])}


@_action("AWSGlue.CreateTable")
def _create_table(body):
    db = body["DatabaseName"].lower()
    table_input = dict(body["TableInput"])
    name = table_input["Name"] = table_input["Name"].lower()
    _require_database(db)
    if _table(db, name) is not None:
        raise GlueError("AlreadyExistsException", f"Table {name} already exists.")
    if body.get("OpenTableFormatInput"):
        raise GlueError(
            "InvalidInputException",
            "OpenTableFormatInput (Glue writing the Iceberg metadata) isn't "
            "supported; create the table with PyIceberg, Spark or Trino",
        )
    if _is_iceberg(table_input):
        metadata_location = (table_input.get("Parameters") or {}).get(
            "metadata_location"
        )
        if not metadata_location:
            raise GlueError(
                "InvalidInputException",
                "an Iceberg table needs Parameters.metadata_location",
            )
        _register(db, name, metadata_location)
    _store().put_table(db, table_input)
    return {}


@_action("AWSGlue.UpdateTable")
def _update_table(body):
    db = body["DatabaseName"].lower()
    table_input = dict(body["TableInput"])
    name = table_input["Name"] = table_input["Name"].lower()
    current = _require_table(db, name)
    if body.get("VersionId") and body["VersionId"] != current.get("VersionId"):
        raise GlueError(
            "ConcurrentModificationException",
            f"Table {name} is at version {current.get('VersionId')}, "
            f"not {body['VersionId']}.",
        )
    if _is_iceberg(current):
        params = dict(table_input.get("Parameters") or {})
        params.setdefault("table_type", current["Parameters"]["table_type"])
        table_input["Parameters"] = params
        new = params.get("metadata_location")
        old = current["Parameters"]["metadata_location"]
        if new and new != old:
            previous = params.get("previous_metadata_location")
            if previous and previous != old:
                raise GlueError(
                    "ConcurrentModificationException",
                    f"Table {name} was committed meanwhile: its metadata is "
                    f"{old}, not {previous}.",
                )
            _drop_registration(db, name)
            _register(db, name, new)
    _store().put_table(db, table_input)
    return {}


def _delete_one(db: str, name: str) -> None:
    table = _require_table(db, name)
    if _is_iceberg(table):
        _drop_registration(db, table["Name"])
    _store().delete_table(db, table["Name"])


@_action("AWSGlue.DeleteTable")
def _delete_table(body):
    _delete_one(body["DatabaseName"].lower(), body["Name"])
    return {}


@_action("AWSGlue.BatchDeleteTable")
def _batch_delete_table(body):
    errors = []
    for name in body["TablesToDelete"]:
        try:
            _delete_one(body["DatabaseName"].lower(), name)
        except GlueError as err:
            errors.append(
                {
                    "TableName": name,
                    "ErrorDetail": {"ErrorCode": err.code, "ErrorMessage": str(err)},
                }
            )
    return {"Errors": errors}


# ---------------------------------------------------------------------------
# Partitions (Hive-style tables)
# ---------------------------------------------------------------------------
def _partition_error(values: list[str], err: GlueError) -> dict:
    return {
        "PartitionValues": values,
        "ErrorDetail": {"ErrorCode": err.code, "ErrorMessage": str(err)},
    }


def _create_partition(db: str, table: str, part_input: dict) -> None:
    if not _store().put_partition(db, table, part_input):
        raise GlueError(
            "AlreadyExistsException",
            f"Partition {part_input['Values']} already exists.",
        )


@_action("AWSGlue.CreatePartition")
def _create_partition_action(body):
    db = body["DatabaseName"].lower()
    table = _require_table(db, body["TableName"])["Name"]
    _create_partition(db, table, body["PartitionInput"])
    return {}


@_action("AWSGlue.BatchCreatePartition")
def _batch_create_partition(body):
    db = body["DatabaseName"].lower()
    table = _require_table(db, body["TableName"])["Name"]
    errors = []
    for part_input in body["PartitionInputList"]:
        try:
            _create_partition(db, table, part_input)
        except GlueError as err:
            errors.append(_partition_error(part_input["Values"], err))
    return {"Errors": errors}


def _require_partition(db: str, table: str, values: list[str]) -> dict:
    partition = _store().partition(db, table, values)
    if partition is None:
        raise _not_found(f"Partition {values} not found.")
    return {**partition, "CatalogId": config.account_id()}


@_action("AWSGlue.GetPartition")
def _get_partition(body):
    db = body["DatabaseName"].lower()
    table = _require_table(db, body["TableName"])["Name"]
    return {"Partition": _require_partition(db, table, body["PartitionValues"])}


@_action("AWSGlue.GetPartitions")
def _get_partitions(body):
    db = body["DatabaseName"].lower()
    table = _require_table(db, body["TableName"])
    partitions = _store().partitions(db, table["Name"])
    if body.get("Expression"):
        keys = table.get("PartitionKeys") or []
        try:
            wanted = compile_expression(
                body["Expression"], {k["Name"]: k.get("Type", "string") for k in keys}
            )
        except ValueError as err:
            raise GlueError(
                "InvalidInputException", f"Unsupported expression: {err}"
            ) from err

        def matches(partition: dict) -> bool:
            row = {k["Name"].lower(): v for k, v in zip(keys, partition["Values"])}
            try:
                return wanted(row)
            except (TypeError, ValueError):
                return False

        partitions = [p for p in partitions if matches(p)]
    segment = body.get("Segment")
    if segment:
        number, total = segment["SegmentNumber"], segment["TotalSegments"]
        partitions = [p for i, p in enumerate(partitions) if i % total == number]
    page, more = _page(partitions, body, 1000)
    return {"Partitions": page, **more}


@_action("AWSGlue.BatchGetPartition")
def _batch_get_partition(body):
    db = body["DatabaseName"].lower()
    table = _require_table(db, body["TableName"])["Name"]
    found = (
        _store().partition(db, table, k["Values"]) for k in body["PartitionsToGet"]
    )
    return {"Partitions": [p for p in found if p], "UnprocessedKeys": []}


def _update_partition(db: str, table: str, values: list[str], part_input: dict) -> None:
    _require_partition(db, table, values)
    if list(part_input.get("Values") or values) != list(values):
        _store().delete_partition(db, table, values)
    _store().put_partition(
        db, table, {**part_input, "Values": part_input.get("Values") or values}
    )


@_action("AWSGlue.UpdatePartition")
def _update_partition_action(body):
    db = body["DatabaseName"].lower()
    table = _require_table(db, body["TableName"])["Name"]
    _update_partition(db, table, body["PartitionValueList"], body["PartitionInput"])
    return {}


@_action("AWSGlue.BatchUpdatePartition")
def _batch_update_partition(body):
    db = body["DatabaseName"].lower()
    table = _require_table(db, body["TableName"])["Name"]
    errors = []
    for entry in body["Entries"]:
        try:
            _update_partition(
                db, table, entry["PartitionValueList"], entry["PartitionInput"]
            )
        except GlueError as err:
            errors.append(_partition_error(entry["PartitionValueList"], err))
    return {"Errors": errors}


@_action("AWSGlue.DeletePartition")
def _delete_partition(body):
    db = body["DatabaseName"].lower()
    table = _require_table(db, body["TableName"])["Name"]
    if not _store().delete_partition(db, table, body["PartitionValues"]):
        raise _not_found(f"Partition {body['PartitionValues']} not found.")
    return {}


@_action("AWSGlue.BatchDeletePartition")
def _batch_delete_partition(body):
    db = body["DatabaseName"].lower()
    table = _require_table(db, body["TableName"])["Name"]
    errors = []
    for key in body["PartitionsToDelete"]:
        if not _store().delete_partition(db, table, key["Values"]):
            err = _not_found(f"Partition {key['Values']} not found.")
            errors.append(_partition_error(key["Values"], err))
    return {"Errors": errors}


# ---------------------------------------------------------------------------
# Column statistics (what Trino's Hive connector records after a write)
# ---------------------------------------------------------------------------
def _stats_target(body: dict) -> tuple[str, str, list[str] | None]:
    db = body["DatabaseName"].lower()
    table = _require_table(db, body["TableName"])["Name"]
    values = body.get("PartitionValues")
    if values is not None:
        _require_partition(db, table, values)
    return db, table, values


@_action("AWSGlue.GetColumnStatisticsForTable")
@_action("AWSGlue.GetColumnStatisticsForPartition")
def _get_column_statistics(body):
    db, table, values = _stats_target(body)
    found = _store().column_stats(db, table, values, body["ColumnNames"])
    return {"ColumnStatisticsList": found, "Errors": []}


@_action("AWSGlue.UpdateColumnStatisticsForTable")
@_action("AWSGlue.UpdateColumnStatisticsForPartition")
def _update_column_statistics(body):
    db, table, values = _stats_target(body)
    for stats in body["ColumnStatisticsList"]:
        _store().put_column_stats(db, table, values, stats)
    return {"Errors": []}


@_action("AWSGlue.DeleteColumnStatisticsForTable")
@_action("AWSGlue.DeleteColumnStatisticsForPartition")
def _delete_column_statistics(body):
    db, table, values = _stats_target(body)
    _store().delete_column_stats(db, table, values, body["ColumnName"])
    return {}


async def _glue_dispatch(request: Request) -> JSONResponse:
    target = request.headers.get("X-Amz-Target", "")
    handler = _ACTIONS.get(target)
    if handler is None:
        print(f"glue: unsupported action {target}", flush=True)
        return JSONResponse(
            {"__type": "InvalidAction", "Message": f"Unsupported action: {target}"},
            status_code=400,
            headers={"x-amzn-errortype": "InvalidAction"},
        )
    body = json.loads(await request.body() or b"{}")
    try:
        result = handler(body)
    except GlueError as err:
        return JSONResponse(
            {"__type": err.code, "Message": str(err)},
            status_code=400,
            headers={"x-amzn-errortype": err.code},
        )
    except Exception as e:
        return JSONResponse(
            {"__type": "InternalServiceException", "Message": str(e)},
            status_code=500,
            headers={"x-amzn-errortype": "InternalServiceException"},
        )
    return JSONResponse(result)


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "actions": sorted(_ACTIONS)})


def create_app() -> Starlette:
    """Build the Starlette ASGI app implementing the Glue Data Catalog wire protocol."""
    # the job actions (CreateJob, StartJobRun, ...) register themselves on import
    importlib.import_module("oblako.engines.glue_catalog.jobs")
    return Starlette(
        routes=[
            Route("/", _health, methods=["GET"]),
            Route("/", _glue_dispatch, methods=["POST"]),
        ]
    )


app = create_app()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if a glue_catalog server is reachable on the port."""
    return is_engine(port, "glue_catalog", timeout)


def start_in_thread(port: int = DEFAULT_PORT) -> str:
    """Start the Glue-API shim in a daemon thread (idempotent)."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "glue_catalog")
        ucfg = uvicorn.Config(
            identify(create_app(), "glue_catalog"),
            host="0.0.0.0",  # Trino's Glue metastore reaches it from its container
            port=port,
            log_level="warning",
        )
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
