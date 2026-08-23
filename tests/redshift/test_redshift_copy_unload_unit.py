"""Unit tests for the COPY/UNLOAD <-> S3 bridge's SQL parsing and rewrite.

Pure parsing only (no engine, S3, or services): the proxy imports this module and
rewrites Redshift ``COPY``/``UNLOAD`` into ``oblako_*`` function calls before the
SQL reaches PostgreSQL. Covers the shapes clients emit (Feast, awswrangler, dbt),
including options the bridge accepts and ignores.
"""

import importlib.util
import pathlib

_PATH = (
    pathlib.Path(__file__).parents[2] / "oblako/images/redshift/proxy/copy_unload.py"
)
_spec = importlib.util.spec_from_file_location("_copy_unload", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# --- detection + statement splitting ---------------------------------------
def test_has_s3_copy_or_unload_gate():
    assert _mod.has_s3_copy_or_unload("COPY t FROM 's3://b/k' FORMAT AS PARQUET")
    assert _mod.has_s3_copy_or_unload("UNLOAD ('SELECT 1') TO 's3://b/k/' PARQUET")
    assert not _mod.has_s3_copy_or_unload("SELECT * FROM t")
    # an s3:// literal with no COPY/UNLOAD verb is not a bridge statement
    assert not _mod.has_s3_copy_or_unload("SELECT 's3://b/k' AS path FROM t")
    # native PostgreSQL COPY (no s3://) passes through
    assert not _mod.has_s3_copy_or_unload("COPY t FROM STDIN WITH CSV")


def test_split_respects_quoted_semicolons():
    sql = "UNLOAD ('SELECT ''a; b'' AS x') TO 's3://b/k/' PARQUET"
    assert _mod.split_statements(sql) == [sql]
    assert _mod.split_statements("SELECT 1;;  ; SELECT 2 ;") == ["SELECT 1", "SELECT 2"]


# --- parsing into pydantic command models ----------------------------------
def test_parse_copy_feast_shape():
    cmd = _mod.parse_copy(
        "COPY feast_entity_df_ab FROM 's3://b/e.parquet' IAM_ROLE 'arn' FORMAT AS PARQUET"
    )
    assert isinstance(cmd, _mod.CopyCommand)
    assert (cmd.table, cmd.uri, cmd.columns, cmd.fmt) == (
        "feast_entity_df_ab",
        "s3://b/e.parquet",
        None,
        "PARQUET",
    )


def test_parse_copy_columns_and_qualified_name():
    cmd = _mod.parse_copy(
        "COPY analytics.events (id, k) FROM 's3://b/k/' IAM_ROLE 'a' FORMAT AS PARQUET"
    )
    assert cmd.table == "analytics.events"
    assert cmd.columns == ["id", "k"]


def test_parse_copy_ignores_native_copy():
    assert _mod.parse_copy("COPY t FROM STDIN WITH CSV") is None


def test_parse_unload_query_uri_and_ignored_options():
    # PARALLEL OFF / ALLOWOVERWRITE / MAXFILESIZE are accepted and ignored;
    # query and destination still parse; '' escapes are unescaped.
    cmd = _mod.parse_unload(
        "UNLOAD ('SELECT a FROM t WHERE s = ''x''') TO 's3://b/out/' "
        "IAM_ROLE 'arn' PARQUET PARALLEL OFF ALLOWOVERWRITE MAXFILESIZE 100 MB"
    )
    assert isinstance(cmd, _mod.UnloadCommand)
    assert cmd.query == "SELECT a FROM t WHERE s = 'x'"
    assert cmd.uri == "s3://b/out/"
    assert cmd.fmt == "PARQUET"


# --- the rewrite the proxy applies -----------------------------------------
def test_rewrite_unload_to_function_call():
    out = _mod.rewrite_copy_unload(
        "UNLOAD ('SELECT * FROM t') TO 's3://b/p/' IAM_ROLE 'x' FORMAT AS PARQUET"
    )
    assert out.startswith("SELECT oblako_unload_to_s3(")
    assert "$ob$SELECT * FROM t$ob$" in out
    assert "$ob$s3://b/p/$ob$" in out


def test_rewrite_copy_to_function_call_with_columns():
    out = _mod.rewrite_copy_unload(
        "COPY t (a, b) FROM 's3://b/k/' IAM_ROLE 'x' FORMAT AS PARQUET"
    )
    assert out.startswith("SELECT oblako_copy_from_s3(")
    assert "$ob$t$ob$" in out
    assert "ARRAY[$ob$a$ob$, $ob$b$ob$]" in out


def test_rewrite_copy_without_columns_passes_null_array():
    out = _mod.rewrite_copy_unload(
        "COPY t FROM 's3://b/k' IAM_ROLE 'x' FORMAT AS PARQUET"
    )
    assert "NULL::text[]" in out


def test_rewrite_keeps_non_bridge_statements_in_a_batch():
    # Feast/redshift-data emit CREATE TEMP TABLE ...; UNLOAD(...) as one batch:
    # the create is kept verbatim so the temp table exists when UNLOAD runs.
    out = _mod.rewrite_copy_unload(
        "CREATE TEMPORARY TABLE _x AS (SELECT 1 AS n); "
        "UNLOAD ('SELECT * FROM _x') TO 's3://b/p/' IAM_ROLE 'x' FORMAT AS PARQUET"
    )
    parts = [s.strip() for s in out.split(";")]
    assert parts[0] == "CREATE TEMPORARY TABLE _x AS (SELECT 1 AS n)"
    assert parts[1].startswith("SELECT oblako_unload_to_s3(")


def test_rewrite_dollar_quote_avoids_collision():
    # a query containing the default $ob$ tag gets a distinct tag, staying valid
    out = _mod.rewrite_copy_unload(
        "UNLOAD ('SELECT $ob$hi$ob$') TO 's3://b/p/' IAM_ROLE 'x' FORMAT AS PARQUET"
    )
    assert "$ob0$SELECT $ob$hi$ob$$ob0$" in out


def test_rewrite_noop_without_copy_unload():
    sql = "SELECT * FROM t WHERE note = 'copy unload s3'"
    assert _mod.rewrite_copy_unload(sql) == sql


# --- CSV / TEXT formats + options ------------------------------------------
def test_detect_format():
    assert _mod._detect_format("IAM_ROLE 'x' FORMAT AS PARQUET") == "PARQUET"
    assert _mod._detect_format("IAM_ROLE 'x' CSV") == "CSV"
    assert _mod._detect_format("IAM_ROLE 'x' FORMAT AS CSV") == "CSV"
    assert _mod._detect_format("IAM_ROLE 'x'") == "TEXT"  # default delimited
    assert _mod._detect_format("FORMAT AS AVRO") == "AVRO"  # unsupported -> named


def test_parse_copy_csv_options():
    cmd = _mod.parse_copy(
        "COPY t FROM 's3://b/k/' IAM_ROLE 'x' CSV DELIMITER ',' "
        "IGNOREHEADER 1 NULL AS '\\N' QUOTE AS '\"'"
    )
    assert cmd.fmt == "CSV"
    assert cmd.delimiter == ","
    assert cmd.ignore_header == 1
    assert cmd.null_as == "\\N"
    assert cmd.quote == '"'


def test_parse_copy_default_text_and_tab_delimiter():
    cmd = _mod.parse_copy("COPY t FROM 's3://b/k' IAM_ROLE 'x' DELIMITER AS '\\t'")
    assert cmd.fmt == "TEXT"
    assert cmd.delimiter == "\t"  # \t unescaped to a real tab


def test_parse_unload_csv_header_and_options():
    cmd = _mod.parse_unload(
        "UNLOAD ('SELECT 1') TO 's3://b/p/' IAM_ROLE 'x' CSV HEADER "
        "DELIMITER ';' NULL AS 'NULL'"
    )
    assert cmd.fmt == "CSV"
    assert cmd.header is True
    assert cmd.delimiter == ";"
    assert cmd.null_as == "NULL"


def test_rewrite_csv_copy_carries_format_and_options():
    out = _mod.rewrite_copy_unload(
        "COPY t FROM 's3://b/k/' IAM_ROLE 'x' CSV DELIMITER ',' IGNOREHEADER 1"
    )
    assert out.startswith("SELECT oblako_copy_from_s3(")
    assert "$ob$CSV$ob$" in out
    # options are passed as a JSON blob the engine function parses
    assert '"delimiter": ","' in out
    assert '"ignore_header": 1' in out
