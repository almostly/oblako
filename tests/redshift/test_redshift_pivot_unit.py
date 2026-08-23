"""Unit tests for the PIVOT/UNPIVOT -> standard SQL rewrite (needs sqlglot)."""

import importlib.util
import pathlib

import pytest

pytest.importorskip("sqlglot")

_PATH = (
    pathlib.Path(__file__).parents[2] / "oblako/images/redshift/proxy/pivot_unpivot.py"
)
_spec = importlib.util.spec_from_file_location("_pivot", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
r = _mod.rewrite_pivot_unpivot


def test_pivot_becomes_conditional_aggregation():
    out = r(
        "SELECT * FROM (SELECT dept, gender, salary FROM emp) AS s "
        "PIVOT (AVG(salary) FOR gender IN ('M' AS male, 'F' AS female))"
    )
    assert "PIVOT" not in out.upper()
    assert "AVG(CASE WHEN gender = 'M' THEN salary END) AS \"male\"" in out
    assert "AVG(CASE WHEN gender = 'F' THEN salary END) AS \"female\"" in out
    assert "GROUP BY dept" in out


def test_unpivot_becomes_lateral_values():
    out = r(
        "SELECT * FROM (SELECT id, q1, q2, q3 FROM sales) AS s "
        "UNPIVOT (amount FOR quarter IN (q1, q2, q3))"
    )
    assert "UNPIVOT" not in out.upper()
    assert "CROSS JOIN LATERAL (VALUES ('q1', s.q1), ('q2', s.q2), ('q3', s.q3))" in out
    assert "AS u(quarter, amount)" in out
    assert "u.amount IS NOT NULL" in out  # Redshift excludes NULLs
    assert "s.id" in out  # non-pivoted column kept


def test_bare_table_source_is_left_untouched():
    # no schema in the proxy for a bare table -> pass through (PostgreSQL errors)
    sql = "SELECT * FROM emp PIVOT (AVG(salary) FOR gender IN ('M', 'F'))"
    assert r(sql) == sql


def test_no_pivot_keyword_untouched():
    sql = "SELECT * FROM t WHERE note = 'contains pivot text'"
    assert r(sql) == sql


def test_unparseable_falls_back():
    sql = "SELECT * FROM (SELECT a FROM t) s PIVOT (((("
    assert r(sql) == sql
