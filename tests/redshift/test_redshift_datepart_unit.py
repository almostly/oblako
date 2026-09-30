"""Unit tests for the bare-datepart quoting rewrite (no services)."""

import importlib.util
import pathlib

_PATH = pathlib.Path(__file__).parents[2] / "oblako/images/redshift/proxy/datepart.py"
_spec = importlib.util.spec_from_file_location("_datepart", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
r = _mod.rewrite_dateparts


def test_bare_dateparts_are_quoted():
    assert (
        r("SELECT DATEADD(month, 1, d), datediff(DAY, a, b), date_part(dow, d)")
        == "SELECT DATEADD('month', 1, d), datediff('day', a, b), date_part('dow', d)"
    )


def test_abbreviations_are_quoted():
    assert r("SELECT dateadd(mon, 2, d)") == "SELECT dateadd('mon', 2, d)"
    assert r("SELECT dateadd( qtr , 1, d)") == "SELECT dateadd( 'qtr' , 1, d)"


def test_quoted_datepart_is_unchanged():
    sql = "SELECT dateadd('month', 1, d), datediff('day', a, b)"
    assert r(sql) == sql


def test_column_argument_is_not_quoted():
    # not a datepart name: leave it to PostgreSQL (and its error) as-is
    sql = "SELECT date_part(unit_col, d) FROM t"
    assert r(sql) == sql


def test_string_literals_are_untouched():
    sql = "SELECT 'dateadd(month, 1, d)' AS s, dateadd(month, 1, d)"
    assert r(sql) == "SELECT 'dateadd(month, 1, d)' AS s, dateadd('month', 1, d)"
    assert r("SELECT 'it''s', datediff(day, a, b)") == (
        "SELECT 'it''s', datediff('day', a, b)"
    )


def test_other_functions_and_identifiers_are_untouched():
    sql = "SELECT my_dateadd(month, 1), extract(month FROM d), month FROM t"
    assert r(sql) == sql


def test_no_date_function_is_a_noop():
    sql = "SELECT month, day FROM calendar"
    assert r(sql) is sql
