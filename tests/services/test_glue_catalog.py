"""Unit tests for the Glue Data Catalog engine (Iceberg REST + its own store)."""

import json
import urllib.parse

import httpx
import pytest
from starlette.testclient import TestClient

import oblako.engines.glue_catalog as glue
from oblako.engines.glue_catalog import _ACTIONS, _iceberg_to_glue_type, _table_to_glue
from oblako.engines.glue_catalog.expression import compile_expression
from oblako.engines.glue_catalog.store import GlueStore
from oblako.services.glue_catalog import GlueCatalogService


def test_actions_registered():
    # The bare minimum surface boto3 code typically hits.
    expected = {
        "AWSGlue.GetDatabases",
        "AWSGlue.GetDatabase",
        "AWSGlue.CreateDatabase",
        "AWSGlue.GetTables",
        "AWSGlue.GetTable",
        "AWSGlue.CreateTable",
        "AWSGlue.UpdateTable",
        "AWSGlue.DeleteTable",
        "AWSGlue.BatchCreatePartition",
        "AWSGlue.GetPartitions",
        "AWSGlue.UpdateColumnStatisticsForTable",
    }
    assert expected.issubset(_ACTIONS.keys())


def test_iceberg_to_glue_type():
    assert _iceberg_to_glue_type("long") == "bigint"
    assert _iceberg_to_glue_type("string") == "string"
    assert _iceberg_to_glue_type("double") == "double"
    assert _iceberg_to_glue_type("timestamptz") == "timestamp"
    # Nested types (struct/list/map) coarse-fallback to string.
    assert _iceberg_to_glue_type({"type": "struct", "fields": []}) == "string"
    # Unknown -> string.
    assert _iceberg_to_glue_type("unknown_type") == "string"


def test_table_to_glue_shape():
    iceberg_json = {
        "metadata-location": "s3://oblako-iceberg/credit/applicants/metadata/00001.json",
        "metadata": {
            "schemas": [
                {
                    "fields": [
                        {"id": 1, "name": "id", "type": "long"},
                        {"id": 2, "name": "score", "type": "double"},
                        {"id": 3, "name": "status", "type": "string"},
                    ]
                }
            ]
        },
    }
    out = _table_to_glue("credit", "applicants", iceberg_json)
    assert out["Name"] == "applicants" and out["DatabaseName"] == "credit"
    assert out["TableType"] == "EXTERNAL_TABLE"
    assert out["Parameters"]["table_type"] == "ICEBERG"
    cols = out["StorageDescriptor"]["Columns"]
    assert cols == [
        {"Name": "id", "Type": "bigint"},
        {"Name": "score", "Type": "double"},
        {"Name": "status", "Type": "string"},
    ]
    assert (
        "s3://oblako-iceberg/credit/applicants" in out["StorageDescriptor"]["Location"]
    )


def test_service_endpoint():
    svc = GlueCatalogService()
    assert svc.endpoint_url == "http://localhost:8486"


# ---------------------------------------------------------------------------
# The engine over an in-memory store and a fake Iceberg REST catalog
# ---------------------------------------------------------------------------
class FakeRest:
    """Just enough of the Iceberg REST catalog: namespaces, tables, register."""

    def __init__(self):
        self.namespaces: dict[str, dict] = {}
        self.tables: dict[tuple[str, str], str] = {}  # -> metadata location

    def request(self, method, url, **kwargs):
        path = urllib.parse.urlsplit(url).path.removeprefix("/v1")
        parts = path.strip("/").split("/")
        body = kwargs.get("json") or {}
        if parts == ["namespaces"]:
            if method == "POST":
                self.namespaces[body["namespace"][0]] = body.get("properties") or {}
                return httpx.Response(200, json={})
            return httpx.Response(
                200, json={"namespaces": [[n] for n in sorted(self.namespaces)]}
            )
        ns = parts[1]
        if ns not in self.namespaces:
            return httpx.Response(404, json={})
        if len(parts) == 2:
            if method == "DELETE":
                del self.namespaces[ns]
                return httpx.Response(204)
            return httpx.Response(
                200, json={"namespace": [ns], "properties": self.namespaces[ns]}
            )
        if parts[2] == "register":
            self.tables[(ns, body["name"])] = body["metadata-location"]
            return httpx.Response(200, json={})
        if len(parts) == 3:
            names = [{"namespace": [ns], "name": t} for n, t in self.tables if n == ns]
            return httpx.Response(200, json={"identifiers": names})
        key = (ns, parts[3])
        if key not in self.tables:
            return httpx.Response(404, json={})
        if method == "DELETE":
            del self.tables[key]
            return httpx.Response(204)
        location = self.tables[key]
        return httpx.Response(
            200,
            json={
                "metadata-location": location,
                "metadata": {
                    "location": location.rsplit("/metadata/", 1)[0],
                    "current-schema-id": 0,
                    "schemas": [
                        {"schema-id": 0, "fields": [{"name": "id", "type": "long"}]}
                    ],
                },
            },
        )


@pytest.fixture
def call(monkeypatch):
    """Call a Glue action on a fresh engine; returns (status, body)."""
    rest = FakeRest()
    monkeypatch.setattr(glue, "_stores", [GlueStore(":memory:")])
    monkeypatch.setattr(glue.httpx, "request", rest.request)
    client = TestClient(glue.create_app())

    def _call(action, body):
        resp = client.post(
            "/",
            content=json.dumps(body),
            headers={"X-Amz-Target": f"AWSGlue.{action}"},
        )
        return resp.status_code, resp.json()

    _call.rest = rest
    return _call


def _hive_table(name="orders"):
    return {
        "Name": name,
        "TableType": "EXTERNAL_TABLE",
        "PartitionKeys": [
            {"Name": "year", "Type": "int"},
            {"Name": "month", "Type": "string"},
        ],
        "StorageDescriptor": {
            "Columns": [{"Name": "aov", "Type": "double"}],
            "Location": "s3://lake/orders/",
        },
        "Parameters": {"classification": "parquet"},
    }


def _partition(year, month):
    return {
        "Values": [str(year), month],
        "StorageDescriptor": {
            "Location": f"s3://lake/orders/year={year}/month={month}/"
        },
    }


def test_database_lifecycle(call):
    assert (
        call("CreateDatabase", {"DatabaseInput": {"Name": "Lake", "Description": "d"}})[
            0
        ]
        == 200
    )
    status, body = call("CreateDatabase", {"DatabaseInput": {"Name": "lake"}})
    assert status == 400 and body["__type"] == "AlreadyExistsException"
    _, body = call("GetDatabase", {"Name": "lake"})
    assert body["Database"]["Description"] == "d"
    assert "lake" in call.rest.namespaces  # also a REST namespace
    for i in range(3):
        call("CreateDatabase", {"DatabaseInput": {"Name": f"db{i}"}})
    _, page = call("GetDatabases", {"MaxResults": 2})
    _, rest = call("GetDatabases", {"MaxResults": 2, "NextToken": page["NextToken"]})
    names = [d["Name"] for d in page["DatabaseList"] + rest["DatabaseList"]]
    assert names == ["db0", "db1", "db2", "lake"] and "NextToken" not in rest
    assert call("DeleteDatabase", {"Name": "lake"})[0] == 200
    assert (
        call("GetDatabase", {"Name": "lake"})[1]["__type"] == "EntityNotFoundException"
    )


def test_hive_table_lifecycle(call):
    status, body = call(
        "CreateTable", {"DatabaseName": "nope", "TableInput": _hive_table()}
    )
    assert body["__type"] == "EntityNotFoundException"
    call("CreateDatabase", {"DatabaseInput": {"Name": "lake"}})
    assert (
        call(
            "CreateTable", {"DatabaseName": "lake", "TableInput": _hive_table("Orders")}
        )[0]
        == 200
    )
    assert (
        call("CreateTable", {"DatabaseName": "lake", "TableInput": _hive_table()})[1][
            "__type"
        ]
        == "AlreadyExistsException"
    )
    _, body = call("GetTable", {"DatabaseName": "lake", "Name": "ORDERS"})
    table = body["Table"]
    assert table["Name"] == "orders" and table["VersionId"] == "1"
    assert table["StorageDescriptor"]["Location"] == "s3://lake/orders/"
    call("CreateTable", {"DatabaseName": "lake", "TableInput": _hive_table("returns")})
    _, body = call("GetTables", {"DatabaseName": "lake", "Expression": "*ord*"})
    assert [t["Name"] for t in body["TableList"]] == ["orders"]
    updated = {**_hive_table(), "Parameters": {"classification": "csv"}}
    status, body = call(
        "UpdateTable", {"DatabaseName": "lake", "TableInput": updated, "VersionId": "7"}
    )
    assert body["__type"] == "ConcurrentModificationException"
    call(
        "UpdateTable", {"DatabaseName": "lake", "TableInput": updated, "VersionId": "1"}
    )
    _, body = call("GetTable", {"DatabaseName": "lake", "Name": "orders"})
    assert body["Table"]["Parameters"] == {"classification": "csv"}
    assert body["Table"]["VersionId"] == "2"
    _, body = call(
        "BatchDeleteTable",
        {"DatabaseName": "lake", "TablesToDelete": ["orders", "ghost"]},
    )
    assert [e["TableName"] for e in body["Errors"]] == ["ghost"]
    assert call("GetTable", {"DatabaseName": "lake", "Name": "orders"})[0] == 400


def test_partitions(call):
    call("CreateDatabase", {"DatabaseInput": {"Name": "lake"}})
    call("CreateTable", {"DatabaseName": "lake", "TableInput": _hive_table()})
    parts = [_partition(y, m) for y in (2025, 2026) for m in ("01", "02", "10")]
    _, body = call(
        "BatchCreatePartition",
        {"DatabaseName": "lake", "TableName": "orders", "PartitionInputList": parts},
    )
    assert body["Errors"] == []
    _, body = call(
        "BatchCreatePartition",
        {
            "DatabaseName": "lake",
            "TableName": "orders",
            "PartitionInputList": parts[:1],
        },
    )
    assert body["Errors"][0]["ErrorDetail"]["ErrorCode"] == "AlreadyExistsException"

    def values(expression=None, **extra):
        req = {"DatabaseName": "lake", "TableName": "orders", **extra}
        if expression:
            req["Expression"] = expression
        return [p["Values"] for p in call("GetPartitions", req)[1]["Partitions"]]

    assert len(values()) == 6
    # year is an int key: compared as a number
    assert values("year > 2025 AND month IN ('01', '10')") == [
        ["2026", "01"],
        ["2026", "10"],
    ]
    assert values("(year = 2025 OR month = '02') AND NOT month LIKE '1%'") == [
        ["2025", "01"],
        ["2025", "02"],
        ["2026", "02"],
    ]
    # Trino reads partitions in segments: each partition exactly once
    segments = [
        values(Segment={"SegmentNumber": i, "TotalSegments": 4}) for i in range(4)
    ]
    assert sorted(v for s in segments for v in s) == sorted(values())
    status, body = call(
        "GetPartitions",
        {"DatabaseName": "lake", "TableName": "orders", "Expression": "year ~ 1"},
    )
    assert body["__type"] == "InvalidInputException"

    key = {"DatabaseName": "lake", "TableName": "orders"}
    moved = {
        **_partition(2026, "02"),
        "StorageDescriptor": {"Location": "s3://elsewhere/"},
    }
    call(
        "UpdatePartition",
        {**key, "PartitionValueList": ["2026", "02"], "PartitionInput": moved},
    )
    _, body = call("GetPartition", {**key, "PartitionValues": ["2026", "02"]})
    assert body["Partition"]["StorageDescriptor"]["Location"] == "s3://elsewhere/"
    _, body = call(
        "BatchGetPartition",
        {
            **key,
            "PartitionsToGet": [{"Values": ["2026", "02"]}, {"Values": ["1999", "01"]}],
        },
    )
    assert [p["Values"] for p in body["Partitions"]] == [["2026", "02"]]
    _, body = call(
        "BatchDeletePartition",
        {
            **key,
            "PartitionsToDelete": [
                {"Values": ["2026", "02"]},
                {"Values": ["1999", "01"]},
            ],
        },
    )
    assert [e["PartitionValues"] for e in body["Errors"]] == [["1999", "01"]]
    assert len(values()) == 5
    call("DeleteTable", {"DatabaseName": "lake", "Name": "orders"})
    call("CreateTable", {"DatabaseName": "lake", "TableInput": _hive_table()})
    assert values() == []  # partitions went with the table


def test_column_statistics(call):
    call("CreateDatabase", {"DatabaseInput": {"Name": "lake"}})
    call("CreateTable", {"DatabaseName": "lake", "TableInput": _hive_table()})
    key = {"DatabaseName": "lake", "TableName": "orders"}
    stats = {
        "ColumnName": "aov",
        "ColumnType": "double",
        "AnalyzedTime": 1790000000,
        "StatisticsData": {
            "Type": "DOUBLE",
            "DoubleColumnStatisticsData": {
                "NumberOfNulls": 0,
                "NumberOfDistinctValues": 5,
            },
        },
    }
    assert call(
        "UpdateColumnStatisticsForTable", {**key, "ColumnStatisticsList": [stats]}
    )[1] == {"Errors": []}
    _, body = call(
        "GetColumnStatisticsForTable", {**key, "ColumnNames": ["aov", "other"]}
    )
    assert body["ColumnStatisticsList"] == [stats]
    call("DeleteColumnStatisticsForTable", {**key, "ColumnName": "aov"})
    assert (
        call("GetColumnStatisticsForTable", {**key, "ColumnNames": ["aov"]})[1][
            "ColumnStatisticsList"
        ]
        == []
    )


def test_iceberg_table_registered_in_rest_catalog(call):
    call("CreateDatabase", {"DatabaseInput": {"Name": "lake"}})
    first = "s3://lake/apps/metadata/00000-a.metadata.json"
    table = {
        "Name": "apps",
        "TableType": "EXTERNAL_TABLE",
        "Parameters": {"table_type": "ICEBERG", "metadata_location": first},
        "StorageDescriptor": {
            "Columns": [{"Name": "id", "Type": "bigint", "Comment": "key"}]
        },
    }
    assert call("CreateTable", {"DatabaseName": "lake", "TableInput": table})[0] == 200
    assert call.rest.tables[("lake", "apps")] == first
    _, body = call("GetTable", {"DatabaseName": "lake", "Name": "apps"})
    got = body["Table"]
    assert got["Parameters"]["metadata_location"] == first
    assert got["StorageDescriptor"]["Location"] == "s3://lake/apps"
    assert got["StorageDescriptor"]["Columns"] == [
        {"Name": "id", "Type": "bigint", "Comment": "key"}
    ]

    # a commit names the metadata it started from; a stale one is refused
    second = "s3://lake/apps/metadata/00001-b.metadata.json"
    stale = {
        **table,
        "Parameters": {
            "table_type": "ICEBERG",
            "metadata_location": second,
            "previous_metadata_location": "s3://old",
        },
    }
    status, body = call("UpdateTable", {"DatabaseName": "lake", "TableInput": stale})
    assert body["__type"] == "ConcurrentModificationException"
    commit = {
        **table,
        "Parameters": {
            "table_type": "ICEBERG",
            "metadata_location": second,
            "previous_metadata_location": first,
        },
    }
    assert (
        call(
            "UpdateTable",
            {
                "DatabaseName": "lake",
                "TableInput": commit,
                "VersionId": got["VersionId"],
            },
        )[0]
        == 200
    )
    assert call.rest.tables[("lake", "apps")] == second

    status, body = call(
        "CreateTable",
        {
            "DatabaseName": "lake",
            "TableInput": {
                **table,
                "Name": "bad",
                "Parameters": {"table_type": "ICEBERG"},
            },
        },
    )
    assert body["__type"] == "InvalidInputException"
    call("DeleteTable", {"DatabaseName": "lake", "Name": "apps"})
    assert ("lake", "apps") not in call.rest.tables


def test_iceberg_table_dropped_elsewhere_disappears(call):
    call("CreateDatabase", {"DatabaseInput": {"Name": "lake"}})
    table = {
        "Name": "apps",
        "Parameters": {
            "table_type": "ICEBERG",
            "metadata_location": "s3://lake/apps/metadata/0.json",
        },
    }
    call("CreateTable", {"DatabaseName": "lake", "TableInput": table})
    del call.rest.tables[
        ("lake", "apps")
    ]  # e.g. DROP TABLE through Trino's iceberg catalog
    assert (
        call("GetTable", {"DatabaseName": "lake", "Name": "apps"})[1]["__type"]
        == "EntityNotFoundException"
    )


def test_unsupported_action(call):
    status, body = call("CreateCrawler", {})
    assert status == 400 and body["__type"] == "InvalidAction"


@pytest.mark.parametrize(
    ("expression", "row", "expected"),
    [
        ("dt = '2026-01-01'", {"dt": "2026-01-01"}, True),
        ("dt <> '2026-01-01'", {"dt": "2026-01-01"}, False),
        ("n BETWEEN 2 AND 10", {"n": "9"}, True),
        ("n BETWEEN 2 AND 10", {"n": "11"}, False),
        ("n NOT IN (1, 2)", {"n": "3"}, True),
        ("dt LIKE '2026-%'", {"dt": "2026-05-01"}, True),
        ("dt IS NULL", {}, True),
        ("`dt` = 'it''s'", {"dt": "it's"}, True),
        ("\"dt\" >= '2026'", {"dt": "2025"}, False),
        ("a = '1' OR b = '1' AND c = '1'", {"a": "1", "b": "0", "c": "0"}, True),
    ],
)
def test_partition_expressions(expression, row, expected):
    assert compile_expression(expression, {"n": "bigint"})(row) is expected


@pytest.mark.parametrize("bad", ["dt =", "dt = 'x' AND", "dt ~ 'x'", "(dt = 'x'"])
def test_partition_expressions_rejected(bad):
    with pytest.raises(ValueError):
        compile_expression(bad, {})
