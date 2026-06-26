"""Unit tests for the Redshift-compat proxy's SQL rewriter (no services)."""

import importlib.util
import pathlib

_PATH = (
    pathlib.Path(__file__).parents[2]
    / "oblako/images/redshift/proxy/redshift_proxy.py"
)
_spec = importlib.util.spec_from_file_location("redshift_proxy", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
rewrite_sql = _mod.rewrite_sql


def _norm(s: str) -> str:
    return " ".join(s.split())


def test_strips_diststyle():
    assert _norm(rewrite_sql("CREATE TABLE t (id int) DISTSTYLE AUTO")) == (
        "CREATE TABLE t (id int)"
    )


def test_strips_all_physical_ddl():
    out = rewrite_sql(
        "CREATE TABLE t (id int ENCODE az64, n varchar(5) ENCODE lzo) "
        "DISTSTYLE KEY DISTKEY (id) COMPOUND SORTKEY (id, n)"
    ).upper()
    for kw in ("ENCODE", "DISTSTYLE", "DISTKEY", "SORTKEY"):
        assert kw not in out
    assert "ID INT" in out and "N VARCHAR(5)" in out


def test_backup_clause_stripped():
    assert "BACKUP" not in rewrite_sql(
        "CREATE TABLE t (a int) DISTSTYLE ALL BACKUP NO"
    ).upper()


def test_temp_and_unlogged_tables_handled():
    # dbt and others create temp tables; those must be stripped too
    assert "DISTSTYLE" not in rewrite_sql(
        "CREATE TEMP TABLE t (a int) DISTSTYLE EVEN"
    ).upper()
    assert "SORTKEY" not in rewrite_sql(
        "CREATE TEMPORARY TABLE t (a int) SORTKEY (a)"
    ).upper()
    assert "DISTKEY" not in rewrite_sql(
        "CREATE UNLOGGED TABLE t (a int) DISTKEY (a)"
    ).upper()


def test_non_ddl_untouched():
    # not a CREATE TABLE -> leave it alone (these words can appear in values)
    sql = "SELECT * FROM t WHERE note = 'distkey  sortkey  encode'"
    assert rewrite_sql(sql) == sql
    sql2 = "SELECT encode(data, 'base64') FROM t"
    assert rewrite_sql(sql2) == sql2
