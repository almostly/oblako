"""DynamoDB vector-search proxy in front of DynamoDB Local.

Every DynamoDB operation is forwarded verbatim to DynamoDB Local, except the
vector-search additions, which DynamoDB Local doesn't implement:

- ``CreateTable`` / ``UpdateTable``: the ``VectorIndexes`` block is captured here
  (DynamoDB Local doesn't understand it) and the rest is forwarded.
- ``DescribeTable``: the response is annotated with the captured ``VectorIndexes``.
- ``SearchVectors``: served here as brute-force KNN — the table is scanned and
  each item's vector attribute is compared to the query with the index's distance
  function (COSINE / DOT_PRODUCT / EUCLIDEAN), returning the top-K by score.

That is oblako's "real behavior, simulated topology": genuine nearest-neighbour
results over really-stored vectors, brute-force instead of AWS's ANN index.
"""

from __future__ import annotations

import json
import math
import threading

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from oblako import ports

_TARGET_PREFIX = "DynamoDB_20120810."
_JSON = "application/x-amz-json-1.0"


def _json(data: dict, status: int = 200) -> Response:
    return Response(json.dumps(data), status_code=status, media_type=_JSON)


def _err(code: str, message: str, status: int = 400) -> Response:
    return Response(
        json.dumps({"__type": code, "message": message}),
        status_code=status,
        media_type=_JSON,
        headers={"X-Amzn-Errortype": code},
    )


class VectorProxy:
    """Proxies DynamoDB to DynamoDB Local and adds vector-search operations."""

    def __init__(self, backend_url: str):
        """Bind to the DynamoDB Local endpoint the proxy forwards to."""
        self.backend = backend_url.rstrip("/")
        self._indexes: dict[str, dict[str, dict]] = {}
        self._lock = threading.Lock()

    async def handle(self, request: Request) -> Response:
        """Dispatch by X-Amz-Target: intercept vector ops, forward the rest."""
        op = request.headers.get("x-amz-target", "").split(".")[-1]
        body = await request.body()
        # DynamoDB Local requires an auth header present (it ignores signature
        # correctness, so a rewritten body is fine); forward the client's.
        auth = {
            name: request.headers[name]
            for name in (
                "Authorization",
                "X-Amz-Date",
                "X-Amz-Security-Token",
                "X-Amz-Content-Sha256",
            )
            if name in request.headers
        }
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            return await self._forward(op, body, auth)
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _err("ValidationException", "invalid JSON body")
        return await handler(payload, auth)

    def _headers(self, op: str, auth: dict) -> dict:
        return {"X-Amz-Target": _TARGET_PREFIX + op, "Content-Type": _JSON, **auth}

    # -- passthrough ---------------------------------------------------------
    async def _forward(self, op: str, body: bytes, auth: dict) -> Response:
        """Forward a request unchanged to DynamoDB Local."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.backend, content=body, headers=self._headers(op, auth)
            )
        return Response(resp.content, status_code=resp.status_code, media_type=_JSON)

    async def _ddb(self, op: str, payload: dict, auth: dict) -> tuple[int, dict]:
        """Call DynamoDB Local with a JSON payload and return (status, body)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.backend,
                content=json.dumps(payload).encode(),
                headers=self._headers(op, auth),
            )
        data = resp.json() if resp.content else {}
        return resp.status_code, data

    # -- table lifecycle (capture VectorIndexes) -----------------------------
    async def op_CreateTable(self, payload: dict, auth: dict) -> Response:
        """Capture VectorIndexes, forward the rest, annotate the response."""
        indexes = payload.pop("VectorIndexes", None)
        key_names = [k["AttributeName"] for k in payload.get("KeySchema", [])]
        status, data = await self._ddb("CreateTable", payload, auth)
        if status == 200 and indexes:
            self._store_indexes(payload["TableName"], indexes, key_names)
            if "TableDescription" in data:
                data["TableDescription"]["VectorIndexes"] = indexes
        return _json(data, status)

    async def op_UpdateTable(self, payload: dict, auth: dict) -> Response:
        """Capture added VectorIndexes, forward the rest, annotate the response."""
        indexes = payload.pop("VectorIndexes", None)
        with self._lock:
            keys = next(
                (i["keys"] for i in self._indexes.get(payload["TableName"], {}).values()),
                [],
            )
        status, data = await self._ddb("UpdateTable", payload, auth)
        if status == 200 and indexes:
            self._store_indexes(payload["TableName"], indexes, keys)
            if "TableDescription" in data:
                data["TableDescription"]["VectorIndexes"] = self._describe_indexes(
                    payload["TableName"]
                )
        return _json(data, status)

    async def op_DescribeTable(self, payload: dict, auth: dict) -> Response:
        """Forward, then annotate the response with captured VectorIndexes."""
        status, data = await self._ddb("DescribeTable", payload, auth)
        indexes = self._describe_indexes(payload.get("TableName", ""))
        if status == 200 and indexes and "Table" in data:
            data["Table"]["VectorIndexes"] = indexes
        return _json(data, status)

    async def op_DeleteTable(self, payload: dict, auth: dict) -> Response:
        """Forward, then drop any captured vector-index metadata."""
        status, data = await self._ddb("DeleteTable", payload, auth)
        if status == 200:
            with self._lock:
                self._indexes.pop(payload.get("TableName", ""), None)
        return _json(data, status)

    # -- vector search -------------------------------------------------------
    async def op_SearchVectors(self, payload: dict, auth: dict) -> Response:
        """Brute-force KNN over the table's stored vectors."""
        table = payload.get("TableName", "")
        index_name = payload.get("IndexName", "")
        with self._lock:
            index = self._indexes.get(table, {}).get(index_name)
        if index is None:
            return _err(
                "ResourceNotFoundException",
                f"no vector index {index_name!r} on table {table!r}",
            )
        try:
            query = [float(x) for x in payload["SearchVector"]]
        except (KeyError, TypeError, ValueError):
            return _err("ValidationException", "SearchVector must be a list of numbers")
        top_k = int(payload.get("TopK", 10))
        attribute, distance = index["attribute"], index["distance"]

        status, scored = await self._score_items(table, attribute, distance, query, auth)
        if status != 200:
            return _err("ResourceNotFoundException", f"table {table!r} not found")
        higher_is_closer = distance in ("COSINE", "DOT_PRODUCT")
        scored.sort(key=lambda s: s[0], reverse=higher_is_closer)
        results = [
            {"Item": _project(item, index, attribute), "Score": score}
            for score, item in scored[:top_k]
        ]
        return _json({"SearchResults": results})

    async def _score_items(
        self, table: str, attribute: str, distance: str, query: list[float], auth: dict
    ) -> tuple[int, list[tuple[float, dict]]]:
        """Scan the table and score every item that has a matching-length vector."""
        scored: list[tuple[float, dict]] = []
        start_key = None
        while True:
            req = {"TableName": table}
            if start_key:
                req["ExclusiveStartKey"] = start_key
            status, data = await self._ddb("Scan", req, auth)
            if status != 200:
                return status, scored
            for item in data.get("Items", []):
                vector = _extract_vector(item.get(attribute))
                if vector is None or len(vector) != len(query):
                    continue
                scored.append((_distance(distance, query, vector), item))
            start_key = data.get("LastEvaluatedKey")
            if not start_key:
                return 200, scored

    # -- index metadata ------------------------------------------------------
    def _store_indexes(self, table: str, indexes: list[dict], keys: list[str]) -> None:
        with self._lock:
            store = self._indexes.setdefault(table, {})
            for idx in indexes:
                store[idx["IndexName"]] = {
                    "attribute": idx["VectorAttribute"]["AttributeName"],
                    "dimensions": idx.get("Dimensions"),
                    "distance": idx.get("DistanceFunction", "COSINE"),
                    "projection": idx.get("Projection", {"ProjectionType": "ALL"}),
                    "keys": list(keys),
                }

    def _describe_indexes(self, table: str) -> list[dict]:
        with self._lock:
            return [
                {
                    "IndexName": name,
                    "VectorAttribute": {"AttributeName": idx["attribute"]},
                    "Dimensions": idx["dimensions"],
                    "DistanceFunction": idx["distance"],
                    "Projection": idx["projection"],
                    "IndexStatus": "ACTIVE",
                }
                for name, idx in self._indexes.get(table, {}).items()
            ]


def _extract_vector(attr_value: dict | None) -> list[float] | None:
    """Read a DynamoDB List-of-Numbers attribute into a float list."""
    if not isinstance(attr_value, dict):
        return None
    if "L" in attr_value:
        try:
            return [float(n["N"]) for n in attr_value["L"]]
        except (KeyError, TypeError, ValueError):
            return None
    if "NS" in attr_value:  # tolerate a number-set representation
        try:
            return [float(n) for n in attr_value["NS"]]
        except (TypeError, ValueError):
            return None
    return None


def _distance(function: str, q: list[float], v: list[float]) -> float:
    """Compute the configured distance/similarity between two vectors."""
    if function == "DOT_PRODUCT":
        return sum(a * b for a, b in zip(q, v))
    if function == "EUCLIDEAN":
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(q, v)))
    # COSINE similarity (default)
    dot = sum(a * b for a, b in zip(q, v))
    nq = math.sqrt(sum(a * a for a in q))
    nv = math.sqrt(sum(b * b for b in v))
    return dot / (nq * nv) if nq and nv else 0.0


def _project(item: dict, index: dict, attribute: str) -> dict:
    """Apply the index projection (the embedding is excluded by default)."""
    projection = index.get("projection", {})
    ptype = projection.get("ProjectionType", "ALL")
    keys = set(index.get("keys", []))
    if ptype == "KEYS_ONLY":
        return {k: v for k, v in item.items() if k in keys}
    if ptype == "INCLUDE":
        allowed = keys | set(projection.get("NonKeyAttributes", []))
        return {k: v for k, v in item.items() if k in allowed}
    return {k: v for k, v in item.items() if k != attribute}  # ALL, minus the vector


def create_app(backend_url: str | None = None) -> Starlette:
    """Create the Starlette proxy app (forwards to DynamoDB Local)."""
    proxy = VectorProxy(backend_url or f"http://localhost:{ports.DYNAMODB}")
    return Starlette(routes=[Route("/", proxy.handle, methods=["POST"])])


app = create_app()
