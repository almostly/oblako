"""Unit tests for the LISTAGG -> string_agg rewrite (no services)."""

import importlib.util
import pathlib

_PATH = pathlib.Path(__file__).parents[2] / "oblako/images/redshift/proxy/listagg.py"
_spec = importlib.util.spec_from_file_location("_listagg", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
r = _mod.rewrite_listagg


def test_within_group_order_by():
    assert (
        r("SELECT LISTAGG(name, ', ') WITHIN GROUP (ORDER BY id) FROM t")
        == "SELECT string_agg((name)::text, ', ' ORDER BY id) FROM t"
    )


def test_no_within_group():
    assert (
        r("SELECT LISTAGG(name, ',') FROM t")
        == "SELECT string_agg((name)::text, ',') FROM t"
    )


def test_default_delimiter_when_omitted():
    assert (
        r("SELECT LISTAGG(name) FROM t") == "SELECT string_agg((name)::text, '') FROM t"
    )


def test_distinct():
    assert (
        r("SELECT LISTAGG(DISTINCT city, '|') WITHIN GROUP (ORDER BY city) FROM t")
        == "SELECT string_agg(DISTINCT (city)::text, '|' ORDER BY city) FROM t"
    )


def test_delimiter_containing_a_comma_is_preserved():
    # the comma inside the delimiter literal must not be treated as an arg
    # separator
    out = r("SELECT LISTAGG(x, ', ') WITHIN GROUP (ORDER BY x) FROM t")
    assert out == "SELECT string_agg((x)::text, ', ' ORDER BY x) FROM t"


def test_expression_argument():
    out = r("SELECT LISTAGG(upper(name), ';') FROM t GROUP BY g")
    assert out == "SELECT string_agg((upper(name))::text, ';') FROM t GROUP BY g"


def test_grouped_by_key():
    out = r(
        "SELECT dept, LISTAGG(emp, ',') WITHIN GROUP (ORDER BY emp) "
        "FROM staff GROUP BY dept"
    )
    assert (
        out == "SELECT dept, string_agg((emp)::text, ',' ORDER BY emp) "
        "FROM staff GROUP BY dept"
    )


def test_inside_string_literal_untouched():
    sql = "SELECT id FROM t WHERE note = 'listagg(x) is a function'"
    assert r(sql) == sql


def test_non_listagg_untouched():
    sql = "SELECT string_agg(x, ',') FROM t"
    assert r(sql) == sql
