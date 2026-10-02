"""Local Amazon S3 Tables (`s3tables`) engine.

S3 Tables is a managed Apache Iceberg catalog: a **table bucket** holds
**namespaces**, each holding **tables** stored as Iceberg. oblako already runs an
Iceberg REST catalog (``tabulario/iceberg-rest`` on :8181, warehouse on S3Proxy),
so this engine serves the real ``s3tables`` wire protocol (rest-json) in-process
and maps its control plane onto that catalog. Unmodified boto3 works:

    import boto3
    t = boto3.client("s3tables")                      # -> http://localhost:8013
    b = t.create_table_bucket(name="lake")["arn"]
    t.create_namespace(tableBucketARN=b, namespace=["sales"])
    t.create_table(tableBucketARN=b, namespace="sales", name="orders", format="ICEBERG",
                   metadata={"iceberg": {"schema": {"fields": [
                       {"name": "id", "type": "long", "required": True},
                       {"name": "amount", "type": "double"}]}}})
    t.get_table_metadata_location(tableBucketARN=b, namespace="sales", name="orders")

Because the tables are real Iceberg tables in the shared catalog, pyiceberg and
the Iceberg REST API can read them. A table bucket + namespace map to a two-level
Iceberg namespace ``[bucket, namespace]``; the local catalog is single-warehouse,
so table buckets are namespace prefixes rather than physically separate catalogs.

Iceberg clients (PyIceberg, Spark, Trino) reach the same tables through the S3
Tables Iceberg REST endpoint, ``/iceberg``, configured as on AWS with the table
bucket's ARN as the warehouse; see ``iceberg.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import httpx
from oblako import config, ports
from oblako.engines.identity import claim_port, identify, is_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from oblako.engines.s3tables import iceberg

DEFAULT_PORT = ports.S3_TABLES
_UNIT = "\x1f"  # Iceberg REST joins multi-level namespaces with the unit separator

_BUCKETS: dict[str, dict] = {}  # name -> {"arn", "createdAt"}
_lock = threading.RLock()


def reset() -> None:
    """Drop the table-bucket registry (used by tests; Iceberg state is separate)."""
    with _lock:
        _BUCKETS.clear()


# ---------------------------------------------------------------------------
# Iceberg REST catalog
# ---------------------------------------------------------------------------
def _iceberg_url() -> str:
    return os.environ.get("OBLAKO_ICEBERG_URL", "http://localhost:8181").rstrip("/")


def _ns_path(bucket: str, namespace: str) -> str:
    """URL-encode a two-level Iceberg namespace [bucket, namespace] for the REST path."""
    return urllib.parse.quote(f"{bucket}{_UNIT}{namespace}", safe="")


def _ice(method: str, path: str, **kw) -> httpx.Response:
    return httpx.request(method, f"{_iceberg_url()}{path}", timeout=8.0, **kw)


# ---------------------------------------------------------------------------
# ARNs / identifiers
# ---------------------------------------------------------------------------
def _bucket_arn(name: str) -> str:
    return f"arn:aws:s3tables:{config.region()}:{config.account_id()}:bucket/{name}"


def _table_arn(bucket: str, namespace: str, name: str) -> str:
    return f"{_bucket_arn(bucket)}/table/{namespace}/{name}"


def _bucket_of(arn: str) -> str:
    """Return the bucket name from a table-bucket ARN (or a bare name)."""
    return arn.split(":bucket/", 1)[-1].split("/")[0] if ":bucket/" in arn else arn


def _version_token(metadata_location: str) -> str:
    return hashlib.sha256((metadata_location or "").encode()).hexdigest()[:16]


def _iceberg_schema(metadata: dict | None) -> dict:
    """Translate an s3tables Iceberg schema to an Iceberg REST schema.

    ``{fields: [{name, type, required}]}`` in, with field ids assigned.
    """
    fields = (((metadata or {}).get("iceberg") or {}).get("schema") or {}).get(
        "fields"
    ) or []
    return {
        "type": "struct",
        "schema-id": 0,
        "fields": [
            {
                "id": f.get("id") or i + 1,
                "name": f["name"],
                "required": bool(f.get("required", False)),
                "type": f.get("type", "string"),
            }
            for i, f in enumerate(fields)
        ],
    }


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------
def _ok(data: dict | None = None, status: int = 200) -> Response:
    return JSONResponse(data or {}, status_code=status)


def _err(code: str, message: str, status: int = 400) -> Response:
    return JSONResponse(
        {"__type": code, "message": message},
        status_code=status,
        headers={"x-amzn-errortype": code},
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
def create_table_bucket(body: dict) -> Response:
    """Handle CreateTableBucket: register a table bucket (an Iceberg namespace root)."""
    name = body["name"]
    with _lock:
        _BUCKETS.setdefault(name, {"arn": _bucket_arn(name), "createdAt": time.time()})
    # a table bucket is the parent Iceberg namespace [bucket]; create it (ignore conflict)
    _ice("POST", "/v1/namespaces", json={"namespace": [name]})
    return _ok({"arn": _bucket_arn(name)})


def list_table_buckets(_body: dict, _segs, query) -> Response:
    """ListTableBuckets, filtered by the optional prefix."""
    prefix = query.get("prefix", "")
    with _lock:
        buckets = [
            {
                "arn": b["arn"],
                "name": n,
                "createdAt": b["createdAt"],
                "ownerAccountId": config.account_id(),
            }
            for n, b in _BUCKETS.items()
            if n.startswith(prefix)
        ]
    return _ok({"tableBuckets": buckets})


def get_table_bucket(arn: str) -> Response:
    """Handle GetTableBucket by ARN (or bare name)."""
    name = _bucket_of(arn)
    with _lock:
        b = _BUCKETS.get(name)
    if not b:
        return _err("NotFoundException", f"no table bucket {name}", 404)
    return _ok(
        {
            "arn": b["arn"],
            "name": name,
            "ownerAccountId": config.account_id(),
            "createdAt": b["createdAt"],
        }
    )


def delete_table_bucket(arn: str) -> Response:
    """Handle DeleteTableBucket: forget the bucket (idempotent)."""
    with _lock:
        _BUCKETS.pop(_bucket_of(arn), None)
    return _ok()


def create_namespace(arn: str, body: dict) -> Response:
    """Handle CreateNamespace: an Iceberg namespace under the bucket."""
    bucket = _bucket_of(arn)
    ns = (
        body["namespace"][0]
        if isinstance(body.get("namespace"), list)
        else body.get("namespace")
    )
    _ice("POST", "/v1/namespaces", json={"namespace": [bucket]})  # ensure parent
    r = _ice("POST", "/v1/namespaces", json={"namespace": [bucket, ns]})
    if r.status_code >= 400 and r.status_code != 409:
        return _err("BadRequestException", f"iceberg: {r.text}", 400)
    return _ok({"tableBucketARN": _bucket_arn(bucket), "namespace": [ns]})


def list_namespaces(arn: str) -> Response:
    """Handle ListNamespaces in a table bucket."""
    bucket = _bucket_of(arn)
    r = _ice("GET", "/v1/namespaces", params={"parent": bucket})
    levels = r.json().get("namespaces", []) if r.status_code < 400 else []
    out = [{"namespace": [n[-1]]} for n in levels if len(n) >= 2 and n[0] == bucket]
    return _ok({"namespaces": out})


def get_namespace(arn: str, ns: str) -> Response:
    """Handle GetNamespace in a table bucket."""
    bucket = _bucket_of(arn)
    r = _ice("GET", f"/v1/namespaces/{_ns_path(bucket, ns)}")
    if r.status_code >= 400:
        return _err("NotFoundException", f"no namespace {ns}", 404)
    return _ok(
        {
            "namespace": [ns],
            "createdAt": time.time(),
            "createdBy": config.account_id(),
            "ownerAccountId": config.account_id(),
        }
    )


def delete_namespace(arn: str, ns: str) -> Response:
    """Handle DeleteNamespace from a table bucket."""
    _ice("DELETE", f"/v1/namespaces/{_ns_path(_bucket_of(arn), ns)}")
    return _ok()


def create_table(arn: str, ns: str, body: dict) -> Response:
    """Handle CreateTable: write real Iceberg metadata through the REST catalog."""
    bucket = _bucket_of(arn)
    name = body["name"]
    r = _ice(
        "POST",
        f"/v1/namespaces/{_ns_path(bucket, ns)}/tables",
        json={"name": name, "schema": _iceberg_schema(body.get("metadata"))},
    )
    if r.status_code >= 400:
        return _err("BadRequestException", f"iceberg createTable: {r.text}", 400)
    loc = r.json().get("metadata-location", "")
    return _ok(
        {"tableARN": _table_arn(bucket, ns, name), "versionToken": _version_token(loc)}
    )


def _load_table(bucket: str, ns: str, name: str) -> dict | None:
    r = _ice(
        "GET",
        f"/v1/namespaces/{_ns_path(bucket, ns)}/tables/{urllib.parse.quote(name)}",
    )
    return r.json() if r.status_code < 400 else None


def list_tables(arn: str, query) -> Response:
    """Handle ListTables in a bucket, optionally within one namespace."""
    bucket = _bucket_of(arn)
    ns = query.get("namespace")
    namespaces = (
        [ns]
        if ns
        else [
            n["namespace"][0]
            for n in json.loads(bytes(list_namespaces(arn).body))["namespaces"]
        ]
    )
    tables = []
    for name_ns in namespaces:
        r = _ice("GET", f"/v1/namespaces/{_ns_path(bucket, name_ns)}/tables")
        for ident in r.json().get("identifiers", []) if r.status_code < 400 else []:
            tables.append(
                {
                    "namespace": [name_ns],
                    "name": ident["name"],
                    "type": "customer",
                    "tableARN": _table_arn(bucket, name_ns, ident["name"]),
                }
            )
    return _ok({"tables": tables})


def get_table(bucket: str, ns: str, name: str) -> Response:
    """Handle GetTable: the table's ARN, format and metadata location."""
    tj = _load_table(bucket, ns, name)
    if tj is None:
        return _err("NotFoundException", f"no table {ns}.{name}", 404)
    loc = tj.get("metadata-location", "")
    warehouse = (tj.get("metadata") or {}).get("location", "")
    now = time.time()
    return _ok(
        {
            "name": name,
            "type": "customer",
            "tableARN": _table_arn(bucket, ns, name),
            "namespace": [ns],
            "versionToken": _version_token(loc),
            "metadataLocation": loc,
            "warehouseLocation": warehouse,
            "format": "ICEBERG",
            "createdAt": now,
            "createdBy": config.account_id(),
            "modifiedAt": now,
            "modifiedBy": config.account_id(),
            "ownerAccountId": config.account_id(),
            "managedByService": "s3tables",
        }
    )


def delete_table(arn: str, ns: str, name: str) -> Response:
    """Handle DeleteTable: drop the Iceberg table (and purge its data)."""
    _ice(
        "DELETE",
        f"/v1/namespaces/{_ns_path(_bucket_of(arn), ns)}/tables/{urllib.parse.quote(name)}",
    )
    return _ok()


def get_table_metadata_location(arn: str, ns: str, name: str) -> Response:
    """Handle GetTableMetadataLocation: where the Iceberg metadata lives."""
    bucket = _bucket_of(arn)
    tj = _load_table(bucket, ns, name)
    if tj is None:
        return _err("NotFoundException", f"no table {ns}.{name}", 404)
    loc = tj.get("metadata-location", "")
    warehouse = (tj.get("metadata") or {}).get("location", "")
    return _ok(
        {
            "versionToken": _version_token(loc),
            "metadataLocation": loc,
            "warehouseLocation": warehouse,
        }
    )


# ---------------------------------------------------------------------------
# rest-json dispatch (path + method carry the operation; ARN path params are
# percent-encoded, so we split the RAW path on literal '/').
# ---------------------------------------------------------------------------
async def _dispatch(request: Request) -> Response:
    method = request.method
    raw = request.scope.get("raw_path") or request.url.path.encode()
    raw_segs = [
        s for s in raw.decode("latin-1").split("?")[0].strip("/").split("/") if s
    ]
    if raw_segs[:2] == ["iceberg", "v1"]:  # the S3 Tables Iceberg REST endpoint
        if raw_segs[2:] == ["config"]:
            return iceberg.config(request)
        return await iceberg.proxy(request, raw_segs[2:])
    segs = [
        urllib.parse.unquote(s)
        for s in raw.decode("latin-1").split("?")[0].strip("/").split("/")
        if s
    ]
    query = dict(request.query_params)
    body = {}
    if method in ("POST", "PUT", "PATCH"):
        data = await request.body()
        if data:
            try:
                body = json.loads(data)
            except json.JSONDecodeError:
                return _err("SerializationException", "invalid JSON body")

    try:
        head = segs[0] if segs else ""
        if head == "buckets":
            if method == "PUT" and len(segs) == 1:
                return create_table_bucket(body)
            if method == "GET" and len(segs) == 1:
                return list_table_buckets(body, segs, query)
            if method == "GET" and len(segs) == 2:
                return get_table_bucket(segs[1])
            if method == "DELETE" and len(segs) == 2:
                return delete_table_bucket(segs[1])
        elif head == "namespaces":
            if method == "PUT" and len(segs) == 2:
                return create_namespace(segs[1], body)
            if method == "GET" and len(segs) == 2:
                return list_namespaces(segs[1])
            if method == "GET" and len(segs) == 3:
                return get_namespace(segs[1], segs[2])
            if method == "DELETE" and len(segs) == 3:
                return delete_namespace(segs[1], segs[2])
        elif head == "tables":
            if method == "PUT" and len(segs) == 3:
                return create_table(segs[1], segs[2], body)
            if method == "GET" and len(segs) == 2:
                return list_tables(segs[1], query)
            if method == "DELETE" and len(segs) == 4:
                return delete_table(segs[1], segs[2], segs[3])
            if method == "GET" and len(segs) == 5 and segs[4] == "metadata-location":
                return get_table_metadata_location(segs[1], segs[2], segs[3])
        elif head == "get-table":
            return get_table(
                query["tableBucketARN"].split("bucket/")[-1].split("/")[0]
                if "bucket/" in query.get("tableBucketARN", "")
                else query.get("tableBucketARN", ""),
                query["namespace"],
                query["name"],
            )
    except KeyError as err:
        return _err("ValidationException", f"missing field {err}")
    except httpx.HTTPError as err:
        return _err("InternalServerError", f"iceberg catalog: {err}", 502)
    # accepted and no-op'd (encryption / policy / metrics / replication / maintenance / tags)
    return _ok()


def create_app() -> Starlette:
    """Create the Starlette app serving the s3tables rest-json protocol."""
    return Starlette(
        routes=[
            Route(
                "/{path:path}",
                _dispatch,
                methods=["GET", "HEAD", "PUT", "POST", "DELETE"],
            )
        ]
    )


app = create_app()
_servers: dict[int, object] = {}


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if the s3tables server is reachable on the port."""
    return is_engine(port, "s3tables", timeout)


def start_in_thread(port: int = DEFAULT_PORT) -> str:
    """Start the s3tables server in a daemon thread (idempotent). Returns its URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "s3tables")
        config = uvicorn.Config(
            identify(create_app(), "s3tables"),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        _servers[port] = server
    deadline = time.time() + 10
    while time.time() < deadline:
        if is_running(port):
            return url
        time.sleep(0.1)
    raise RuntimeError(f"s3tables server did not start on port {port}")
