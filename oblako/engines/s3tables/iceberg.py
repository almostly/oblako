"""The S3 Tables Iceberg REST endpoint: ``/iceberg/v1/...`` on the s3tables engine.

On AWS, Iceberg clients reach S3 Tables at ``https://s3tables.<region>.amazonaws.com/
iceberg`` with the table bucket's ARN as the ``warehouse``. The config call answers
with that ARN as the path ``prefix``, and every later call carries it, so a client
addresses a table as ``namespace.table`` inside one table bucket.

oblako keeps every table in one shared Iceberg REST catalog (``tabulario/
iceberg-rest`` on :8181), where a table bucket is the first level of a two-level
namespace ``[bucket, namespace]``. This module serves the AWS shape on top of it:
it resolves the bucket from the prefix, rewrites ``namespace`` to ``[bucket,
namespace]`` on the way in (path, query and JSON body) and back on the way out,
and proxies everything else unchanged. So PyIceberg configured for S3 Tables
(``type=rest``, ``warehouse=<bucket ARN>``, SigV4 on) works against
``http://localhost:8013/iceberg`` with only the URI changed, and the tables stay
visible to Trino, Glue and the ``s3tables`` API.
"""

from __future__ import annotations

import json
import os
import urllib.parse

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from oblako import ports

_UNIT = "\x1f"  # Iceberg REST joins multi-level namespaces with the unit separator
_HOP_BY_HOP = {"content-length", "transfer-encoding", "connection", "content-encoding"}


def _iceberg_url() -> str:
    return os.environ.get("OBLAKO_ICEBERG_URL", "http://localhost:8181").rstrip("/")


def _client_s3_endpoint() -> str:
    """S3 endpoint the *client* writes data files to (oblako's S3 front)."""
    return os.environ.get("OBLAKO_S3_CLIENT_ENDPOINT", f"http://localhost:{ports.S3}")


def _bucket_of(arn: str) -> str:
    """Return the table bucket name from its ARN (or a bare name)."""
    return arn.split(":bucket/", 1)[-1].split("/")[0] if ":bucket/" in arn else arn


def _error(status: int, kind: str, message: str) -> Response:
    """Build an Iceberg REST error response."""
    return JSONResponse(
        {"error": {"message": message, "type": kind, "code": status}},
        status_code=status,
    )


# ---------------------------------------------------------------------------
# Namespace translation: ns <-> [bucket, ns]
# ---------------------------------------------------------------------------
def _ns_in(bucket: str, encoded: str) -> str:
    """Path segment for a client namespace, prefixed with the bucket level."""
    levels = urllib.parse.unquote(encoded).split(_UNIT)
    return urllib.parse.quote(_UNIT.join([bucket, *levels]), safe="")


def _body_in(bucket: str, value):
    """Prefix every ``namespace`` list in a request body with the bucket."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "namespace" and isinstance(item, list):
                out[key] = [bucket, *item]
            else:
                out[key] = _body_in(bucket, item)
        return out
    if isinstance(value, list):
        return [_body_in(bucket, item) for item in value]
    return value


def _strip(bucket: str, levels: list) -> list:
    return levels[1:] if levels and levels[0] == bucket else levels


def _body_out(bucket: str, value):
    """Drop the bucket level from namespaces in a catalog response."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "namespace" and isinstance(item, list):
                out[key] = _strip(bucket, item)
            elif key == "namespaces" and isinstance(item, list):
                out[key] = [
                    _strip(bucket, n) if isinstance(n, list) else n for n in item
                ]
            else:
                out[key] = _body_out(bucket, item)
        return out
    if isinstance(value, list):
        return [_body_out(bucket, item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def config(request: Request) -> Response:
    """GET /iceberg/v1/config?warehouse=<bucket ARN>: hand back the ARN as prefix."""
    warehouse = request.query_params.get("warehouse", "")
    if not warehouse:
        return _error(
            400, "BadRequestException", "warehouse (table bucket ARN) is required"
        )
    return JSONResponse(
        {
            "defaults": {},
            "overrides": {
                "prefix": urllib.parse.quote(warehouse, safe=""),
                # Data files go to oblako's S3; the client's own AWS credentials
                # chain supplies the (unchecked) keys, as it would on AWS.
                "s3.endpoint": _client_s3_endpoint(),
                "s3.region": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            },
        }
    )


async def proxy(request: Request, raw_segments: list[str]) -> Response:
    """Serve /iceberg/v1/<prefix>/... against the shared catalog's /v1/...

    ``raw_segments`` is the still-percent-encoded path after ``iceberg/v1``,
    starting with the prefix.
    """
    if not raw_segments:
        return _error(404, "NoSuchEndpointException", "missing table bucket prefix")
    bucket = _bucket_of(urllib.parse.unquote(raw_segments[0]))
    rest = list(raw_segments[1:])
    # /namespaces/{ns}/... : the segment after "namespaces" is a namespace
    if len(rest) >= 2 and rest[0] == "namespaces":
        rest[1] = _ns_in(bucket, rest[1])
    params = dict(request.query_params)
    if rest == ["namespaces"] and request.method == "GET":
        parent = params.get("parent")
        params["parent"] = _UNIT.join([bucket, parent]) if parent else bucket

    content = None
    if request.method in ("POST", "PUT"):
        data = await request.body()
        if data:
            try:
                content = json.dumps(_body_in(bucket, json.loads(data))).encode()
            except json.JSONDecodeError:
                return _error(400, "BadRequestException", "invalid JSON body")
        if rest == ["namespaces"] and request.method == "POST":
            # make sure the bucket level exists before creating a child under it
            httpx.post(
                f"{_iceberg_url()}/v1/namespaces",
                json={"namespace": [bucket]},
                timeout=8.0,
            )

    upstream = await _send(request.method, "/v1/" + "/".join(rest), params, content)
    body = upstream.content
    if body and upstream.headers.get("content-type", "").startswith("application/json"):
        try:
            body = json.dumps(_body_out(bucket, json.loads(body))).encode()
        except json.JSONDecodeError:
            pass
    headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    return Response(body, status_code=upstream.status_code, headers=headers)


async def _send(
    method: str, path: str, params: dict, content: bytes | None
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.request(
            method,
            f"{_iceberg_url()}{path}",
            params=params,
            content=content,
            headers={"Content-Type": "application/json"} if content else None,
        )
