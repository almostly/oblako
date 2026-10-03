"""Unit tests for the wire proxy's AVG rewrite (no engine needed)."""

import importlib.util
import pathlib

_PATH = (
    pathlib.Path(__file__).parents[2] / "oblako/images/redshift/proxy/integer_avg.py"
)
_spec = importlib.util.spec_from_file_location("_integer_avg", _PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
rewrite_avg = _mod.rewrite_avg


def test_calls_are_qualified():
    assert rewrite_avg("SELECT avg(x) FROM t") == "SELECT pg_oblako.avg(x) FROM t"
    assert rewrite_avg("SELECT AVG ( x ) OVER () FROM t") == (
        "SELECT pg_oblako.avg ( x ) OVER () FROM t"
    )


def test_literals_identifiers_and_comments_are_left_alone():
    sql = "SELECT 'avg(x)', \"avg\"(1), t.avg(x), myavg(x), avg_ms FROM t -- avg(x)"
    assert rewrite_avg(sql) == sql
    assert rewrite_avg("/* avg(1) */ SELECT 1") == "/* avg(1) */ SELECT 1"


def test_a_column_named_avg_and_definitions_are_left_alone():
    assert rewrite_avg("SELECT avg FROM t") == "SELECT avg FROM t"
    assert rewrite_avg("CREATE FUNCTION avg(int)") == "CREATE FUNCTION avg(int)"
