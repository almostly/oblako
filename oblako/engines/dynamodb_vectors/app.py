"""DynamoDB proxy in front of DynamoDB Local: vector search and tagging.

Every DynamoDB operation is forwarded to DynamoDB Local, except what it lacks:

- **Vector indexes** (``VectorIndexes`` on ``CreateTable``, ``VectorIndexUpdates``
  on ``UpdateTable``): captured here with their ``SearchSchema`` (one ``HASH``
  vector index partition key, any ``INLINE_FILTER`` attributes), projection,
  dimensions and distance function, and reported by ``DescribeTable``.
  Attribute definitions that only a search schema uses are kept here too, since
  DynamoDB Local rejects definitions no key uses.
- **SearchVectors**: brute-force k-NN over the table's stored vectors, with the
  released API's rules: ``TopK`` 1 to 100, the query's dimensions must match the
  index, ``SearchConditionExpression`` takes equality on search-schema
  attributes and must name the partition key when there is one, items without
  the partition key are not indexed, the vector is left out of results unless
  ``ProjectionExpression`` names it, and only projected attributes come back.
- **Writes** to a table with a vector index are validated as DynamoDB does: a
  vector of the wrong dimensions, or a partition-key value of the wrong type, is
  rejected (``PutItem``, ``BatchWriteItem`` and ``TransactWriteItems`` puts).
- **Tags**: ``TagResource`` / ``UntagResource`` / ``ListTagsOfResource`` and
  ``CreateTable(Tags=...)``, which DynamoDB Local doesn't implement.

Index definitions and tags persist in ``~/.oblako/dynamodb/proxy.json``, so they
survive a restart along with DynamoDB Local's data.

That is oblako's "real behavior, simulated topology": genuine nearest-neighbour
results over really-stored vectors, brute force instead of AWS's ANN index.
"""

from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from oblako import ports

_TARGET_PREFIX = "DynamoDB_20120810."
_JSON = "application/x-amz-json-1.0"
STATE = Path.home() / ".oblako" / "dynamodb" / "proxy.json"
MAX_VECTOR_INDEXES = 5
_INVALID = "One or more parameter values were invalid: "
_CONDITION = re.compile(r"^\s*(#?[A-Za-z0-9_.-]+)\s*=\s*(:[A-Za-z0-9_]+)\s*$")
_ATTR_TYPES = {"S": "S", "N": "N", "B": "B"}


def _json(data: dict, status: int = 200) -> Response:
    """Return a DynamoDB JSON response."""
    return Response(json.dumps(data), status_code=status, media_type=_JSON)


def _err(code: str, message: str, status: int = 400) -> Response:
    """Return a DynamoDB-style error response."""
    full = f"com.amazonaws.dynamodb.v20120810#{code}"
    return Response(
        json.dumps({"__type": full, "message": message}),
        status_code=status,
        media_type=_JSON,
        headers={"X-Amzn-Errortype": code},
    )


class _Invalid(Exception):
    """A request DynamoDB would reject; carries the error code."""

    def __init__(self, message: str, code: str = "ValidationException"):
        """Carry the message and the DynamoDB error code."""
        super().__init__(message)
        self.code = code


def _table_name(name_or_arn: str) -> str:
    """Return the table name from a name or a table (or index) ARN."""
    if name_or_arn.startswith("arn:"):
        return name_or_arn.split(":table/", 1)[-1].split("/", 1)[0]
    return name_or_arn


# ---------------------------------------------------------------------------
# Persistent state: vector indexes, extra attribute definitions, tags
# ---------------------------------------------------------------------------
class _State:
    """Per-table vector indexes, extra attribute definitions and tags."""

    def __init__(self, path: Path):
        """Bind to the JSON state file."""
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> dict:
        """Return the whole state, or {} if the file is missing or corrupt."""
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def save(self, data: dict) -> None:
        """Write the state atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def table(self, name: str) -> dict:
        """Return the stored metadata for a table."""
        return self.load().get(name, {})

    def update(self, name: str, **fields) -> None:
        """Merge fields into a table's stored metadata."""
        with self._lock:
            data = self.load()
            data[name] = {**data.get(name, {}), **fields}
            self.save(data)

    def drop(self, name: str) -> None:
        """Remove a table's stored metadata."""
        with self._lock:
            data = self.load()
            data.pop(name, None)
            self.save(data)


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------
class VectorProxy:
    """Proxies DynamoDB to DynamoDB Local and adds what it lacks."""

    def __init__(self, backend_url: str, state_path: Path | None = None):
        """Bind to the DynamoDB Local endpoint the proxy forwards to."""
        self.backend = backend_url.rstrip("/")
        self.state = _State(state_path or STATE)

    async def handle(self, request: Request) -> Response:
        """Dispatch by X-Amz-Target: intercept what's added, forward the rest."""
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
        try:
            return await handler(payload, auth)
        except _Invalid as e:
            return _err(e.code, str(e))

    def _headers(self, op: str, auth: dict) -> dict:
        """Return request headers for a DynamoDB Local call."""
        return {"X-Amz-Target": _TARGET_PREFIX + op, "Content-Type": _JSON, **auth}

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

    def _annotate(self, table: str, description: dict) -> dict:
        """Add the vector indexes and extra attribute definitions to a description."""
        meta = self.state.table(table)
        indexes = meta.get("indexes", {})
        if indexes:
            arn = description.get("TableArn", "")
            description["VectorIndexes"] = [
                _describe_index(name, spec, arn) for name, spec in indexes.items()
            ]
        extra = meta.get("attribute_definitions", [])
        if extra:
            known = {
                a["AttributeName"] for a in description.get("AttributeDefinitions", [])
            }
            description.setdefault("AttributeDefinitions", []).extend(
                a for a in extra if a["AttributeName"] not in known
            )
        return description

    # -------------------------------------------------------------------------
    # Table lifecycle
    # -------------------------------------------------------------------------
    async def op_CreateTable(self, payload: dict, auth: dict) -> Response:
        """Capture VectorIndexes and Tags, forward the rest, annotate the response."""
        table = payload["TableName"]
        indexes = payload.pop("VectorIndexes", None) or []
        tags = payload.pop("Tags", None) or []
        key_names = _key_names(payload)
        definitions = payload.get("AttributeDefinitions", [])
        if indexes:
            if len(indexes) > MAX_VECTOR_INDEXES:
                raise _Invalid(
                    _INVALID + "VectorIndex count exceeds the per-table limit of 5"
                )
            if payload.get("BillingMode") != "PAY_PER_REQUEST":
                raise _Invalid(
                    _INVALID + "Vector indexes require the PAY_PER_REQUEST billing mode"
                )
            for index in indexes:
                _check_index(index, definitions)
        # DynamoDB Local rejects definitions that no key uses: keep them here
        extra = [a for a in definitions if a["AttributeName"] not in key_names]
        payload["AttributeDefinitions"] = [
            a for a in definitions if a["AttributeName"] in key_names
        ]
        status, data = await self._ddb("CreateTable", payload, auth)
        if status == 200:
            keys = [k["AttributeName"] for k in payload.get("KeySchema", [])]
            self.state.drop(table)
            self.state.update(
                table,
                keys=keys,
                indexes={i["IndexName"]: _index_spec(i) for i in indexes},
                attribute_definitions=extra,
                tags={t["Key"]: t["Value"] for t in tags},
            )
            if "TableDescription" in data:
                self._annotate(table, data["TableDescription"])
        return _json(data, status)

    async def op_UpdateTable(self, payload: dict, auth: dict) -> Response:
        """Apply VectorIndexUpdates (one Create or Delete), forward the rest."""
        table = _table_name(payload["TableName"])
        updates = payload.pop("VectorIndexUpdates", None) or []
        meta = self.state.table(table)
        indexes = dict(meta.get("indexes", {}))
        extra = list(meta.get("attribute_definitions", []))
        if len(updates) > 1:
            raise _Invalid(
                "Subscriber limit exceeded: Only 1 online index can be created or "
                "deleted simultaneously per table",
                code="LimitExceededException",
            )
        status, current = await self._ddb("DescribeTable", {"TableName": table}, auth)
        if status != 200:
            return _json(current, status)
        description = current["Table"]
        key_names = _key_names(description)
        definitions = payload.get("AttributeDefinitions", [])
        for update in updates:
            if update.get("Create"):
                create = update["Create"]
                if len(indexes) >= MAX_VECTOR_INDEXES:
                    raise _Invalid(
                        _INVALID + "VectorIndex count exceeds the per-table limit of 5"
                    )
                if create["IndexName"] in indexes:
                    raise _Invalid(f"Vector index {create['IndexName']} already exists")
                known = description.get("AttributeDefinitions", []) + extra
                _check_index(create, definitions + known)
                indexes[create["IndexName"]] = _index_spec(create)
            elif update.get("Delete"):
                name = update["Delete"]["IndexName"]
                if name not in indexes:
                    raise _Invalid(
                        f"Requested resource not found: Vector index {name}",
                        code="ResourceNotFoundException",
                    )
                indexes.pop(name)
        # attribute definitions only a search schema uses stay here; the rest
        # (keys of the table or of a GSI being created) go to DynamoDB Local
        for gsi in payload.get("GlobalSecondaryIndexUpdates", []) or []:
            if gsi.get("Create"):
                key_names |= {k["AttributeName"] for k in gsi["Create"]["KeySchema"]}
        names = {a["AttributeName"] for a in extra}
        extra += [
            a
            for a in definitions
            if a["AttributeName"] not in key_names and a["AttributeName"] not in names
        ]
        forwarded = {k: v for k, v in payload.items() if k != "AttributeDefinitions"}
        kept = [a for a in definitions if a["AttributeName"] in key_names]
        if kept:
            forwarded["AttributeDefinitions"] = kept
        if set(forwarded) - {"TableName"}:
            status, data = await self._ddb("UpdateTable", forwarded, auth)
            if status != 200:
                return _json(data, status)
            description = data["TableDescription"]
        self.state.update(
            table,
            keys=[k["AttributeName"] for k in description.get("KeySchema", [])],
            indexes=indexes,
            attribute_definitions=extra,
        )
        return _json({"TableDescription": self._annotate(table, description)})

    async def op_DescribeTable(self, payload: dict, auth: dict) -> Response:
        """Forward, then add the vector indexes and extra attribute definitions."""
        table = _table_name(payload.get("TableName", ""))
        status, data = await self._ddb(
            "DescribeTable", {**payload, "TableName": table}, auth
        )
        if status == 200 and "Table" in data:
            self._annotate(table, data["Table"])
        return _json(data, status)

    async def op_DeleteTable(self, payload: dict, auth: dict) -> Response:
        """Forward, then drop the table's vector indexes and tags."""
        table = _table_name(payload.get("TableName", ""))
        status, data = await self._ddb(
            "DeleteTable", {**payload, "TableName": table}, auth
        )
        if status == 200:
            self.state.drop(table)
        return _json(data, status)

    # -------------------------------------------------------------------------
    # Tags
    # -------------------------------------------------------------------------
    async def _require_table(self, arn: str, auth: dict) -> str:
        """Return the table name for an ARN; raise ResourceNotFound if it's missing."""
        table = _table_name(arn)
        status, _ = await self._ddb("DescribeTable", {"TableName": table}, auth)
        if status != 200:
            raise _Invalid(
                f"Requested resource not found: ResourcArn: {arn} not found",
                code="ResourceNotFoundException",
            )
        return table

    async def op_TagResource(self, payload: dict, auth: dict) -> Response:
        """Add or overwrite tags on a table."""
        table = await self._require_table(payload.get("ResourceArn", ""), auth)
        tags = dict(self.state.table(table).get("tags", {}))
        tags.update({t["Key"]: t["Value"] for t in payload.get("Tags", [])})
        self.state.update(table, tags=tags)
        return _json({})

    async def op_UntagResource(self, payload: dict, auth: dict) -> Response:
        """Remove tags from a table."""
        table = await self._require_table(payload.get("ResourceArn", ""), auth)
        tags = dict(self.state.table(table).get("tags", {}))
        for key in payload.get("TagKeys", []):
            tags.pop(key, None)
        self.state.update(table, tags=tags)
        return _json({})

    async def op_ListTagsOfResource(self, payload: dict, auth: dict) -> Response:
        """List a table's tags."""
        table = await self._require_table(payload.get("ResourceArn", ""), auth)
        tags = self.state.table(table).get("tags", {})
        return _json({"Tags": [{"Key": k, "Value": v} for k, v in tags.items()]})

    # -------------------------------------------------------------------------
    # Write validation
    # -------------------------------------------------------------------------
    def _validate_item(self, table: str, item: dict) -> None:
        """Check an item's vectors and partition keys against the vector indexes."""
        meta = self.state.table(table)
        definitions = {
            a["AttributeName"]: a["AttributeType"]
            for a in meta.get("attribute_definitions", [])
        }
        for name, spec in meta.get("indexes", {}).items():
            value = item.get(spec["attribute"])
            if value is not None:
                vector = _extract_vector(value)
                if vector is None or len(vector) != spec["dimensions"]:
                    raise _Invalid(
                        _INVALID + f"Vector attribute {spec['attribute']} for index "
                        f"{name} must be a list of {spec['dimensions']} numbers"
                    )
            partition = spec.get("partition_key")
            if partition and partition in item:
                expected = definitions.get(partition)
                if expected and expected not in item[partition]:
                    raise _Invalid(
                        _INVALID + f"Type mismatch for vector index partition key "
                        f"{partition}: expected {expected}"
                    )

    async def op_PutItem(self, payload: dict, auth: dict) -> Response:
        """Validate vectors against the table's vector indexes, then forward."""
        self._validate_item(
            _table_name(payload.get("TableName", "")), payload.get("Item", {})
        )
        return await self._forward("PutItem", json.dumps(payload).encode(), auth)

    async def op_BatchWriteItem(self, payload: dict, auth: dict) -> Response:
        """Validate the batch's puts, then forward."""
        for table, requests in payload.get("RequestItems", {}).items():
            for request in requests:
                if "PutRequest" in request:
                    self._validate_item(
                        _table_name(table), request["PutRequest"]["Item"]
                    )
        return await self._forward("BatchWriteItem", json.dumps(payload).encode(), auth)

    async def op_TransactWriteItems(self, payload: dict, auth: dict) -> Response:
        """Validate the transaction's puts, then forward."""
        for action in payload.get("TransactItems", []):
            if "Put" in action:
                put = action["Put"]
                self._validate_item(_table_name(put["TableName"]), put["Item"])
        return await self._forward(
            "TransactWriteItems", json.dumps(payload).encode(), auth
        )

    # -------------------------------------------------------------------------
    # Vector search
    # -------------------------------------------------------------------------
    async def op_SearchVectors(self, payload: dict, auth: dict) -> Response:
        """Brute-force KNN over the table's stored vectors."""
        table = _table_name(payload.get("TableName", ""))
        index_name = payload.get("IndexName", "")
        meta = self.state.table(table)
        spec = meta.get("indexes", {}).get(index_name)
        if spec is None:
            raise _Invalid(
                f"Requested resource not found: vector index {index_name} on "
                f"table {table}",
                code="ResourceNotFoundException",
            )
        top_k = int(payload.get("TopK", 0))
        if not 1 <= top_k <= 100:
            raise _Invalid(_INVALID + "TopK must be between 1 and 100")
        try:
            query = _query_vector(payload["SearchVector"])
        except (KeyError, TypeError, ValueError):
            raise _Invalid(_INVALID + "SearchVector must be a list of numbers")
        if len(query) != spec["dimensions"]:
            raise _Invalid(
                _INVALID + f"SearchVector has {len(query)} dimensions; the index "
                f"has {spec['dimensions']}"
            )
        names = payload.get("ExpressionAttributeNames", {})
        values = payload.get("ExpressionAttributeValues", {})
        conditions = _parse_condition(
            payload.get("SearchConditionExpression"), names, values, spec
        )
        wanted = _parse_projection(payload.get("ProjectionExpression"), names)

        attribute, distance = spec["attribute"], spec["distance"]
        partition = spec.get("partition_key")
        scored: list[tuple[float, dict]] = []
        async for item in self._scan(table, auth):
            if partition and partition not in item:
                continue  # not indexed without its partition key
            if any(item.get(k) != v for k, v in conditions.items()):
                continue
            vector = _extract_vector(item.get(attribute))
            if vector is None or len(vector) != len(query):
                continue
            scored.append((_distance(distance, query, vector), item))
        # AWS score semantics: DOT_PRODUCT returns k highest; COSINE/EUCLIDEAN
        # are distances (0 = identical) and return k smallest.
        scored.sort(key=lambda s: s[0], reverse=distance == "DOT_PRODUCT")
        keys = set(meta.get("keys", []))
        results = [
            {"Item": _project(item, spec, keys, wanted), "Score": score}
            for score, item in scored[:top_k]
        ]
        return _json({"SearchResults": results})

    async def _scan(self, table: str, auth: dict):
        """Yield every item in a table, following Scan pagination."""
        start_key = None
        while True:
            request: dict = {"TableName": table}
            if start_key:
                request["ExclusiveStartKey"] = start_key
            status, data = await self._ddb("Scan", request, auth)
            if status != 200:
                raise _Invalid(
                    f"Requested resource not found: table {table}",
                    code="ResourceNotFoundException",
                )
            for item in data.get("Items", []):
                yield item
            start_key = data.get("LastEvaluatedKey")
            if not start_key:
                return


# ---------------------------------------------------------------------------
# Index definitions
# ---------------------------------------------------------------------------
def _key_names(description: dict) -> set[str]:
    """Return every attribute a table or its secondary indexes use as a key."""
    names = {k["AttributeName"] for k in description.get("KeySchema", [])}
    for group in ("GlobalSecondaryIndexes", "LocalSecondaryIndexes"):
        for index in description.get(group, []) or []:
            names |= {k["AttributeName"] for k in index.get("KeySchema", [])}
    return names


def _check_index(index: dict, definitions: list[dict]) -> None:
    """Reject an index definition DynamoDB would reject."""
    declared = {a["AttributeName"] for a in definitions}
    schema = index.get("SearchSchema", []) or []
    partitions = [e for e in schema if e["SearchSchemaElementType"] == "HASH"]
    if len(partitions) > 1:
        raise _Invalid(
            _INVALID + "A vector index can have at most one HASH search schema element"
        )
    for element in schema:
        if element["AttributeName"] not in declared:
            raise _Invalid(
                _INVALID + f"Search schema attribute {element['AttributeName']} "
                "is not defined in AttributeDefinitions"
            )
    if not 1 <= int(index.get("Dimensions", 0)) <= 4096:
        raise _Invalid(_INVALID + "Dimensions must be between 1 and 4096")


def _index_spec(index: dict) -> dict:
    """Return the stored spec for a vector index definition."""
    schema = index.get("SearchSchema", []) or []
    return {
        "attribute": index["VectorAttribute"]["AttributeName"],
        "dimensions": int(index["Dimensions"]),
        "distance": index.get("DistanceFunction", "COSINE"),
        "projection": index.get("Projection", {"ProjectionType": "ALL"}),
        "search_schema": schema,
        "partition_key": next(
            (
                e["AttributeName"]
                for e in schema
                if e["SearchSchemaElementType"] == "HASH"
            ),
            None,
        ),
    }


def _describe_index(name: str, spec: dict, table_arn: str) -> dict:
    """Return the DescribeTable entry for a vector index."""
    description = {
        "IndexName": name,
        "VectorAttribute": {"AttributeName": spec["attribute"]},
        "Projection": spec["projection"],
        "Dimensions": spec["dimensions"],
        "DistanceFunction": spec["distance"],
        "IndexStatus": "ACTIVE",
        "IndexArn": f"{table_arn}/index/{name}",
    }
    if spec.get("search_schema"):
        description["SearchSchema"] = spec["search_schema"]
    return description


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------
def _parse_condition(
    expression: str | None, names: dict, values: dict, spec: dict
) -> dict[str, dict]:
    """Parse ``a = :x AND b = :y`` over search-schema attributes into {attr: value}."""
    schema = {e["AttributeName"] for e in spec.get("search_schema", [])}
    partition = spec.get("partition_key")
    conditions: dict[str, dict] = {}
    if expression:
        for clause in re.split(r"\s+AND\s+", expression.strip(), flags=re.IGNORECASE):
            m = _CONDITION.match(clause)
            if m is None:
                raise _Invalid(
                    "Invalid SearchConditionExpression: only equality (=) on search "
                    f"schema attributes, joined by AND, is supported: {clause!r}"
                )
            token, placeholder = m.groups()
            name = names.get(token, token) if token.startswith("#") else token
            if token.startswith("#") and token not in names:
                raise _Invalid(
                    f"An expression attribute name used is not defined: {token}"
                )
            if placeholder not in values:
                raise _Invalid(
                    f"An expression attribute value used is not defined: {placeholder}"
                )
            if name not in schema:
                raise _Invalid(
                    f"Invalid SearchConditionExpression: {name} is not in the vector "
                    "index search schema"
                )
            conditions[name] = values[placeholder]
    if partition and partition not in conditions:
        raise _Invalid(
            "Invalid SearchConditionExpression: the vector index partition key "
            f"{partition} must be specified"
        )
    return conditions


def _parse_projection(expression: str | None, names: dict) -> list[str] | None:
    """Return the attribute names in a projection expression, or None for all."""
    if not expression:
        return None
    out = []
    for token in (t.strip() for t in expression.split(",")):
        if token.startswith("#"):
            if token not in names:
                raise _Invalid(
                    f"An expression attribute name used is not defined: {token}"
                )
            token = names[token]
        out.append(token)
    return out


# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------
def _query_vector(raw: list) -> list[float]:
    """Read a SearchVector into floats: AttributeValue elements or raw numbers."""
    out = []
    for element in raw:
        if isinstance(element, dict):
            out.append(float(element["N"]))
        else:
            out.append(float(element))
    return out


def _extract_vector(attr_value: dict | None) -> list[float] | None:
    """Read a DynamoDB List-of-Numbers attribute into a float list."""
    if not isinstance(attr_value, dict) or "L" not in attr_value:
        return None
    try:
        return [float(n["N"]) for n in attr_value["L"]]
    except (KeyError, TypeError, ValueError):
        return None


def _distance(function: str, q: list[float], v: list[float]) -> float:
    """Compute the configured distance/similarity between two vectors."""
    if function == "DOT_PRODUCT":
        return sum(a * b for a, b in zip(q, v))
    if function == "EUCLIDEAN":
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(q, v)))
    # COSINE distance (default): 0 = identical, 2 = opposite, matching AWS
    dot = sum(a * b for a, b in zip(q, v))
    nq = math.sqrt(sum(a * a for a in q))
    nv = math.sqrt(sum(b * b for b in v))
    similarity = dot / (nq * nv) if nq and nv else 0.0
    return 1.0 - similarity


def _project(item: dict, spec: dict, keys: set[str], wanted: list[str] | None) -> dict:
    """Return what the index projects; the vector only when asked for by name."""
    projection = spec.get("projection", {})
    ptype = projection.get("ProjectionType", "ALL")
    attribute = spec["attribute"]
    if ptype == "ALL":
        projected = set(item)
    else:
        projected = (
            keys
            | {attribute}
            | {e["AttributeName"] for e in spec.get("search_schema", [])}
        )
        if ptype == "INCLUDE":
            projected |= set(projection.get("NonKeyAttributes", []))
    if wanted is None:
        selected = projected - {attribute}
    else:
        selected = projected & set(wanted)
    return {k: v for k, v in item.items() if k in selected}


def create_app(
    backend_url: str | None = None, state_path: Path | None = None
) -> Starlette:
    """Create the Starlette proxy app (forwards to DynamoDB Local)."""
    proxy = VectorProxy(backend_url or f"http://localhost:{ports.DYNAMODB}", state_path)
    return Starlette(routes=[Route("/", proxy.handle, methods=["POST"])])


app = create_app()
