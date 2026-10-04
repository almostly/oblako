"""Unit tests for the Iceberg DDL rewrite and parsing (no containers)."""

import importlib.util
import pathlib

import pytest

_PATH = (
    pathlib.Path(__file__).parents[2] / "oblako/images/redshift/proxy/iceberg_tables.py"
)
_spec = importlib.util.spec_from_file_location("_iceberg", _PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_external_schema_becomes_a_call():
    out = _mod.rewrite_iceberg(
        "CREATE EXTERNAL SCHEMA lake FROM DATA CATALOG DATABASE 'sales' "
        "IAM_ROLE default CREATE EXTERNAL DATABASE IF NOT EXISTS;"
    )
    assert out.startswith("SELECT pg_oblako.create_external_schema(")
    assert "$ice$lake$ice$, $ice$sales$ice$, true, false" in out


def test_other_external_schemas_are_left_alone():
    sql = "CREATE EXTERNAL SCHEMA pg FROM POSTGRES DATABASE 'x' URI 'h'"
    assert _mod.rewrite_iceberg(sql) == sql


def test_create_table_using_iceberg_is_parsed():
    p = _mod.parse_create_iceberg(
        "CREATE TABLE IF NOT EXISTS lake.items (id int, price decimal(5, 2), "
        "ship date) USING ICEBERG LOCATION 's3://b/items/' "
        "PARTITIONED BY (bucket(16, id), year(ship)) "
        "TABLE PROPERTIES ('compression_type'='snappy');"
    )
    assert p["name"] == "lake.items"
    assert p["if_not_exists"]
    assert p["columns"] == "id int, price decimal(5, 2), ship date"
    assert p["location"] == "s3://b/items/"
    assert p["partitioned"] == "bucket(16, id), year(ship)"
    assert p["properties"] == "'compression_type'='snappy'"
    assert p["query"] is None


def test_partitioned_by_without_parentheses():
    p = _mod.parse_create_iceberg(
        "CREATE TABLE s.l (id int, d varchar) USING ICEBERG LOCATION 's3://b/l/' "
        "PARTITIONED BY d;"
    )
    assert p["partitioned"] == "d"


def test_ctas_keeps_the_query():
    p = _mod.parse_create_iceberg(
        "CREATE TABLE s.backup (a, b) USING ICEBERG LOCATION 's3://b/x/' "
        "TABLE PROPERTIES ('format-version'='3') AS SELECT a, b FROM s.orders;"
    )
    assert p["columns"] == "a, b"
    assert p["query"] == "SELECT a, b FROM s.orders"


def test_a_plain_create_table_is_not_iceberg():
    assert _mod.parse_create_iceberg("CREATE TABLE t (a int) DISTSTYLE ALL") is None
    sql = "CREATE TABLE t (a int)"
    assert _mod.rewrite_iceberg(sql) == sql


def test_show_table():
    out = _mod.rewrite_iceberg('SHOW TABLE lake."Items"')
    assert out == (
        'SELECT pg_oblako.show_table($ice$lake."Items"$ice$) '
        'AS "Show Table DDL statement"'
    )


def test_literal_survives_its_own_tag():
    assert _mod._literal("a $ice$ b") == "$ice1$a $ice$ b$ice1$"


def test_split_name_folds_unquoted_parts():
    assert _mod.split_name('Lake."My Table"') == ["lake", "My Table"]


def test_partition_transforms():
    assert _mod.parse_partitions("bucket(16, id), year(ship), region") == [
        ("id", "bucket", 16),
        ("ship", "year", None),
        ("region", "identity", None),
    ]


@pytest.mark.parametrize(
    "spec",
    ["bucket(16, d), year(d)", "bucket(id)", "year(4, d)", "void(d)"],
)
def test_bad_partition_specs(spec):
    with pytest.raises(ValueError):
        _mod.parse_partitions(spec)


def test_properties():
    assert _mod.parse_properties("'format-version'='2', 'compression_type'='GZIP'") == {
        "format-version": "2",
        "compression_type": "gzip",
    }
    bad_ones = (
        "'format-version'='1'",
        "'format-version'='3'",  # refused by Redshift Serverless too (2026-10)
        "'compression_type'='lz4'",
        "'owner'='x'",
    )
    for bad in bad_ones:
        with pytest.raises(ValueError):
            _mod.parse_properties(bad)
