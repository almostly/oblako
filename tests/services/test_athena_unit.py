"""Unit tests for the Athena engine: CTAS translation, statement types, workgroups."""

import json

import pytest
from starlette.testclient import TestClient

from oblako.engines.athena.app import AthenaExecutor, create_app
from oblako.engines.athena.ctas import rewrite, statement_type
from oblako.engines.athena.workgroups import DEFAULT_OUTPUT, WorkGroupError, WorkGroups

# ---------------------------------------------------------------------------
# CTAS
# ---------------------------------------------------------------------------
_WRANGLER_CTAS = """CREATE TABLE "lake"."temp_table_1"
WITH(
    external_location = 's3://results/temp_table_1',
    write_compression = 'SNAPPY',
    format = 'PARQUET'
)
AS SELECT order_month, avg(aov) FROM orders GROUP BY 1"""


def test_wrangler_ctas_keeps_location_and_moves_compression_to_session():
    ctas = rewrite(_WRANGLER_CTAS, "s3://results", "q1")
    assert ctas is not None and not ctas.iceberg
    assert ctas.table == '"lake"."temp_table_1"'
    assert "external_location = 's3://results/temp_table_1'" in ctas.sql
    assert "format = 'PARQUET'" in ctas.sql
    assert "write_compression" not in ctas.sql
    assert ctas.session == {"awsdatacatalog.compression_codec": "SNAPPY"}
    assert ctas.sql.endswith("AS SELECT order_month, avg(aov) FROM orders GROUP BY 1")


def test_ctas_without_location_lands_under_the_results():
    sql = "CREATE TABLE t WITH (format = 'textfile', field_delimiter = ',', partitioned_by = ARRAY['a', 'b']) AS SELECT 1 AS x, 'p' AS a, 'q' AS b"
    ctas = rewrite(sql, "s3://results/", "q2")
    assert ctas is not None
    assert "external_location = 's3://results/tables/q2'" in ctas.sql
    assert "textfile_field_separator = ','" in ctas.sql
    assert "partitioned_by = ARRAY['a', 'b']" in ctas.sql
    assert "format = 'TEXTFILE'" in ctas.sql


def test_iceberg_ctas_runs_in_the_iceberg_catalog():
    sql = (
        "CREATE TABLE lake.apps WITH (table_type = 'ICEBERG', is_external = false, "
        "location = 's3://lake/apps', partitioning = ARRAY['day(ts)'], "
        "vacuum_min_snapshots_to_keep = 5, write_compression = 'zstd') AS SELECT 1 AS id"
    )
    ctas = rewrite(sql, "s3://results", "q3")
    assert ctas is not None and ctas.iceberg
    assert "location = 's3://lake/apps'" in ctas.sql
    assert "partitioning = ARRAY['day(ts)']" in ctas.sql
    for gone in ("table_type", "is_external", "vacuum_", "external_location"):
        assert gone not in ctas.sql
    assert ctas.session == {"iceberg.compression_codec": "ZSTD"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "CREATE TABLE t (a int)",
        "CREATE TABLE t WITH (format = 'PARQUET')",
        "CREATE TABLE t AS SELECT 1",
    ],
)
def test_not_a_ctas_with_properties(sql):
    assert rewrite(sql, "s3://results", "q") is None


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT 1", "DML"),
        ("  (SELECT 1)", "DML"),
        ("WITH a AS (SELECT 1) SELECT * FROM a", "DML"),
        ("INSERT INTO t VALUES (1)", "DML"),
        ("CREATE TABLE t WITH (format='PARQUET') AS SELECT 1", "DDL"),
        ("SHOW TABLES", "DDL"),
        ("EXPLAIN SELECT 1", "UTILITY"),
        ("", "UTILITY"),
    ],
)
def test_statement_type(sql, kind):
    assert statement_type(sql) == kind


# ---------------------------------------------------------------------------
# Workgroups
# ---------------------------------------------------------------------------
def test_primary_has_a_local_output_location(tmp_path):
    groups = WorkGroups(tmp_path / "wg.json")
    primary = groups.get("primary")
    assert (
        primary["Configuration"]["ResultConfiguration"]["OutputLocation"]
        == DEFAULT_OUTPUT
    )
    assert primary["Configuration"]["EnforceWorkGroupConfiguration"] is False
    # the query's own location wins unless the workgroup enforces its own
    assert groups.output_location("primary", "s3://mine/") == "s3://mine/"
    assert groups.output_location("primary", None) == DEFAULT_OUTPUT


def test_workgroup_lifecycle_persists(tmp_path):
    path = tmp_path / "wg.json"
    groups = WorkGroups(path)
    groups.create(
        {
            "Name": "analysts",
            "Description": "team",
            "Configuration": {
                "ResultConfiguration": {"OutputLocation": "s3://team/"},
                "EnforceWorkGroupConfiguration": True,
            },
        }
    )
    with pytest.raises(WorkGroupError):
        groups.create({"Name": "analysts"})
    assert groups.output_location("analysts", "s3://mine/") == "s3://team/"

    reloaded = WorkGroups(path)
    assert [g["Name"] for g in reloaded.summaries()] == ["analysts", "primary"]
    reloaded.update(
        {
            "WorkGroup": "analysts",
            "ConfigurationUpdates": {
                "EnforceWorkGroupConfiguration": False,
                "ResultConfigurationUpdates": {"RemoveOutputLocation": True},
            },
        }
    )
    with pytest.raises(WorkGroupError, match="No output location"):
        reloaded.output_location("analysts", None)
    reloaded.update({"WorkGroup": "analysts", "State": "DISABLED"})
    with pytest.raises(WorkGroupError, match="disabled"):
        reloaded.output_location("analysts", "s3://mine/")
    reloaded.delete("analysts")
    with pytest.raises(WorkGroupError):
        reloaded.get("analysts")
    with pytest.raises(WorkGroupError):
        reloaded.delete("primary")


def test_workgroup_api(tmp_path):
    client = TestClient(create_app(AthenaExecutor(WorkGroups(tmp_path / "wg.json"))))

    def call(op, body):
        resp = client.post(
            "/",
            content=json.dumps(body),
            headers={"X-Amz-Target": f"AmazonAthena.{op}"},
        )
        return resp.status_code, resp.json()

    status, body = call("GetWorkGroup", {"WorkGroup": "primary"})
    assert status == 200 and body["WorkGroup"]["State"] == "ENABLED"
    status, body = call("GetWorkGroup", {"WorkGroup": "ghost"})
    assert status == 400 and body["__type"] == "InvalidRequestException"
    assert call("CreateWorkGroup", {"Name": "etl"})[0] == 200
    _, body = call("ListWorkGroups", {})
    assert [g["Name"] for g in body["WorkGroups"]] == ["etl", "primary"]
    # etl has no output location: a query without one is refused, as on AWS
    status, body = call(
        "StartQueryExecution", {"QueryString": "SELECT 1", "WorkGroup": "etl"}
    )
    assert status == 400 and "No output location" in body["message"]
