"""Local Amazon S3 Vectors (`s3vectors`) engine.

S3 Vectors is AWS's dedicated, cheap-at-rest vector store: a **vector bucket**
holds **indexes** (each with a fixed dimension + distance metric), and you
``PutVectors`` (key + float32 vector + JSON metadata) then ``QueryVectors`` for
the nearest neighbours, optionally filtering on metadata. oblako serves the real
`s3vectors` wire protocol (rest-json) in-process, so unmodified boto3 works:

    import boto3
    v = boto3.client("s3vectors")                 # -> http://localhost:8012
    v.create_vector_bucket(vectorBucketName="docs")
    v.create_index(vectorBucketName="docs", indexName="emb",
                   dataType="float32", dimension=3, distanceMetric="cosine")
    v.put_vectors(vectorBucketName="docs", indexName="emb", vectors=[
        {"key": "a", "data": {"float32": [0.1, 0.2, 0.3]}, "metadata": {"lang": "en"}},
    ])
    v.query_vectors(vectorBucketName="docs", indexName="emb", topK=5,
                    queryVector={"float32": [0.1, 0.2, 0.29]},
                    filter={"lang": "en"}, returnMetadata=True, returnDistance=True)

Storage is in-process (lost on restart); ``QueryVectors`` is brute-force k-NN,
not ANN. Pairs with Bedrock embeddings, which produce the vectors it stores.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.request

from oblako import ports
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

DEFAULT_PORT = ports.S3_VECTORS
_ACCOUNT = "000000000000"
_REGION = "us-east-1"

# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------
# buckets:  name -> {"arn": str}
# indexes:  (bucket, index) -> {"arn","dataType","dimension","distanceMetric",
#                               "vectors": {key: {"data":[float], "metadata":{}}}}
_BUCKETS: dict[str, dict] = {}
_INDEXES: dict[tuple[str, str], dict] = {}
_lock = threading.RLock()


def reset() -> None:
    """Drop all buckets/indexes (used by tests)."""
    with _lock:
        _BUCKETS.clear()
        _INDEXES.clear()


# ---------------------------------------------------------------------------
# ARNs
# ---------------------------------------------------------------------------
def _bucket_arn(name: str) -> str:
    return f"arn:aws:s3vectors:{_REGION}:{_ACCOUNT}:bucket/{name}"


def _index_arn(bucket: str, index: str) -> str:
    return f"arn:aws:s3vectors:{_REGION}:{_ACCOUNT}:bucket/{bucket}/index/{index}"


def _parse_index_arn(arn: str) -> tuple[str, str] | None:
    tail = arn.split(":bucket/", 1)[-1]
    if "/index/" not in tail:
        return None
    bucket, index = tail.split("/index/", 1)
    return bucket, index


def _resolve_index(payload: dict) -> tuple[str, str] | None:
    """(bucket, index) from an indexArn, or the bucket-name + index-name pair."""
    if payload.get("indexArn"):
        return _parse_index_arn(payload["indexArn"])
    bucket, index = payload.get("vectorBucketName"), payload.get("indexName")
    return (bucket, index) if bucket and index else None


# ---------------------------------------------------------------------------
# k-NN + metadata filter
# ---------------------------------------------------------------------------
def _distance(metric: str, q: list[float], v: list[float]) -> float:
    """Distance for the index's metric (smaller = closer), matching AWS.

    cosine: 1 - cosine similarity (0 identical). euclidean: L2 distance.
    """
    if metric == "euclidean":
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(q, v)))
    dot = sum(a * b for a, b in zip(q, v))
    nq = math.sqrt(sum(a * a for a in q))
    nv = math.sqrt(sum(b * b for b in v))
    return 1.0 - (dot / (nq * nv)) if nq and nv else 1.0


_OPS = {
    "$eq": lambda a, b: a == b,
    "$ne": lambda a, b: a != b,
    "$gt": lambda a, b: a is not None and a > b,
    "$gte": lambda a, b: a is not None and a >= b,
    "$lt": lambda a, b: a is not None and a < b,
    "$lte": lambda a, b: a is not None and a <= b,
    "$in": lambda a, b: a in b,
    "$nin": lambda a, b: a not in b,
    "$exists": lambda a, b: (a is not None) == bool(b),
}


def _matches(meta: dict, flt: dict) -> bool:
    """Evaluate an S3 Vectors metadata filter (Mongo-style) against metadata."""
    if not flt:
        return True
    meta = meta or {}
    for key, cond in flt.items():
        if key == "$and":
            if not all(_matches(meta, c) for c in cond):
                return False
        elif key == "$or":
            if not any(_matches(meta, c) for c in cond):
                return False
        elif isinstance(cond, dict) and any(k.startswith("$") for k in cond):
            for op, operand in cond.items():
                fn = _OPS.get(op)
                if fn is None or not fn(meta.get(key), operand):
                    return False
        elif meta.get(key) != cond:  # implicit equality
            return False
    return True


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------
def _ok(data: dict | None = None) -> Response:
    return JSONResponse(data or {})


def _err(code: str, message: str, status: int = 400) -> Response:
    return JSONResponse(
        {"__type": code, "message": message},
        status_code=status,
        headers={"x-amzn-errortype": code},
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
def op_CreateVectorBucket(p: dict) -> Response:
    name = p["vectorBucketName"]
    with _lock:
        _BUCKETS.setdefault(name, {"arn": _bucket_arn(name), "created": time.time()})
    return _ok({"vectorBucketArn": _bucket_arn(name)})


def op_ListVectorBuckets(p: dict) -> Response:
    with _lock:
        buckets = [
            {"vectorBucketName": n, "vectorBucketArn": b["arn"]}
            for n, b in _BUCKETS.items()
        ]
    return _ok({"vectorBuckets": buckets})


def op_GetVectorBucket(p: dict) -> Response:
    name = p.get("vectorBucketName") or p.get("vectorBucketArn", "").split("bucket/")[-1]
    with _lock:
        if name not in _BUCKETS:
            return _err("NotFoundException", f"no vector bucket {name}", 404)
    return _ok({"vectorBucket": {"vectorBucketName": name, "vectorBucketArn": _bucket_arn(name)}})


def op_DeleteVectorBucket(p: dict) -> Response:
    name = p.get("vectorBucketName") or p.get("vectorBucketArn", "").split("bucket/")[-1]
    with _lock:
        _BUCKETS.pop(name, None)
        for key in [k for k in _INDEXES if k[0] == name]:
            _INDEXES.pop(key, None)
    return _ok()


def op_CreateIndex(p: dict) -> Response:
    bucket = p.get("vectorBucketName") or p.get("vectorBucketArn", "").split("bucket/")[-1]
    index = p["indexName"]
    with _lock:
        if bucket not in _BUCKETS:
            return _err("NotFoundException", f"no vector bucket {bucket}", 404)
        _INDEXES[(bucket, index)] = {
            "arn": _index_arn(bucket, index),
            "dataType": p.get("dataType", "float32"),
            "dimension": int(p["dimension"]),
            "distanceMetric": p.get("distanceMetric", "cosine"),
            "vectors": {},
        }
    return _ok({"indexArn": _index_arn(bucket, index)})


def op_ListIndexes(p: dict) -> Response:
    bucket = p.get("vectorBucketName") or p.get("vectorBucketArn", "").split("bucket/")[-1]
    prefix = p.get("prefix") or ""
    with _lock:
        indexes = [
            {
                "vectorBucketName": b,
                "indexName": i,
                "indexArn": idx["arn"],
            }
            for (b, i), idx in _INDEXES.items()
            if b == bucket and i.startswith(prefix)
        ]
    return _ok({"indexes": indexes})


def op_GetIndex(p: dict) -> Response:
    ref = _resolve_index(p)
    with _lock:
        idx = _INDEXES.get(ref) if ref else None
        if not idx:
            return _err("NotFoundException", "no such index", 404)
        return _ok(
            {
                "index": {
                    "vectorBucketName": ref[0],
                    "indexName": ref[1],
                    "indexArn": idx["arn"],
                    "dataType": idx["dataType"],
                    "dimension": idx["dimension"],
                    "distanceMetric": idx["distanceMetric"],
                }
            }
        )


def op_DeleteIndex(p: dict) -> Response:
    ref = _resolve_index(p)
    with _lock:
        if ref:
            _INDEXES.pop(ref, None)
    return _ok()


def op_PutVectors(p: dict) -> Response:
    ref = _resolve_index(p)
    with _lock:
        idx = _INDEXES.get(ref) if ref else None
        if not idx:
            return _err("NotFoundException", "no such index", 404)
        for item in p.get("vectors", []):
            data = (item.get("data") or {}).get("float32") or []
            if len(data) != idx["dimension"]:
                return _err(
                    "ValidationException",
                    f"vector {item.get('key')!r} has dimension {len(data)}, "
                    f"index expects {idx['dimension']}",
                )
            idx["vectors"][item["key"]] = {
                "data": [float(x) for x in data],
                "metadata": item.get("metadata") or {},
            }
    return _ok()


def op_GetVectors(p: dict) -> Response:
    ref = _resolve_index(p)
    with _lock:
        idx = _INDEXES.get(ref) if ref else None
        if not idx:
            return _err("NotFoundException", "no such index", 404)
        want_data, want_meta = p.get("returnData"), p.get("returnMetadata")
        out = []
        for key in p.get("keys", []):
            rec = idx["vectors"].get(key)
            if rec is None:
                continue
            vec = {"key": key}
            if want_data:
                vec["data"] = {"float32": rec["data"]}
            if want_meta:
                vec["metadata"] = rec["metadata"]
            out.append(vec)
    return _ok({"vectors": out})


def op_ListVectors(p: dict) -> Response:
    ref = _resolve_index(p)
    with _lock:
        idx = _INDEXES.get(ref) if ref else None
        if not idx:
            return _err("NotFoundException", "no such index", 404)
        want_data, want_meta = p.get("returnData"), p.get("returnMetadata")
        out = []
        for key, rec in idx["vectors"].items():
            vec = {"key": key}
            if want_data:
                vec["data"] = {"float32": rec["data"]}
            if want_meta:
                vec["metadata"] = rec["metadata"]
            out.append(vec)
    return _ok({"vectors": out})


def op_DeleteVectors(p: dict) -> Response:
    ref = _resolve_index(p)
    with _lock:
        idx = _INDEXES.get(ref) if ref else None
        if not idx:
            return _err("NotFoundException", "no such index", 404)
        for key in p.get("keys", []):
            idx["vectors"].pop(key, None)
    return _ok()


def op_QueryVectors(p: dict) -> Response:
    ref = _resolve_index(p)
    with _lock:
        idx = _INDEXES.get(ref) if ref else None
        if not idx:
            return _err("NotFoundException", "no such index", 404)
        metric = idx["distanceMetric"]
        query = [float(x) for x in (p.get("queryVector") or {}).get("float32", [])]
        top_k = int(p.get("topK", 10))
        flt = p.get("filter") or {}
        scored = [
            (_distance(metric, query, rec["data"]), key, rec)
            for key, rec in idx["vectors"].items()
            if _matches(rec["metadata"], flt)
        ]
        scored.sort(key=lambda s: s[0])
        want_meta, want_dist = p.get("returnMetadata"), p.get("returnDistance")
        out = []
        for dist, key, rec in scored[:top_k]:
            vec = {"key": key}
            if want_dist:
                vec["distance"] = dist
            if want_meta:
                vec["metadata"] = rec["metadata"]
            out.append(vec)
    return _ok({"vectors": out, "distanceMetric": metric})


# Ops accepted and no-op'd for local use (policy / tags / encryption).
_NOOP = {
    "PutVectorBucketPolicy", "GetVectorBucketPolicy", "DeleteVectorBucketPolicy",
    "TagResource", "UntagResource", "ListTagsForResource",
}
_HANDLERS = {name[3:]: fn for name, fn in globals().items() if name.startswith("op_")}


# ---------------------------------------------------------------------------
# ASGI app
# ---------------------------------------------------------------------------
async def _dispatch(request: Request) -> Response:
    op = request.url.path.lstrip("/")
    if op in _NOOP:
        return _ok()
    handler = _HANDLERS.get(op)
    if handler is None:
        return _err("UnknownOperationException", f"unsupported op {op}", 404)
    raw = await request.body()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _err("SerializationException", "invalid JSON body")
    try:
        return handler(payload)
    except KeyError as err:
        return _err("ValidationException", f"missing field {err}")


def create_app() -> Starlette:
    """Create the Starlette app serving the s3vectors rest-json protocol."""
    return Starlette(routes=[Route("/{op:path}", _dispatch, methods=["POST", "GET"])])


app = create_app()
_servers: dict[int, object] = {}


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """True if the s3vectors server is reachable on the port."""
    try:
        req = urllib.request.Request(
            f"http://localhost:{port}/ListVectorBuckets", data=b"{}", method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # any HTTP response means the server is up
    except Exception:
        return False


def start_in_thread(port: int = DEFAULT_PORT) -> str:
    """Start the s3vectors server in a daemon thread (idempotent). Returns its URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        config = uvicorn.Config(
            create_app(), host="127.0.0.1", port=port, log_level="warning"
        )
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        _servers[port] = server
    deadline = time.time() + 10
    while time.time() < deadline:
        if is_running(port):
            return url
        time.sleep(0.1)
    raise RuntimeError(f"s3vectors server did not start on port {port}")
