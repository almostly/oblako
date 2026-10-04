"""Unit tests for SUPER (PartiQL) dot-navigation rewriting (no services).

The proxy imports ``super_nav`` and rewrites ``data.a.b`` chains rooted at a known
SUPER column into a jsonb path, learning SUPER columns from the DDL it relays.
"""

import importlib.util
import pathlib

import pytest

_PATH = pathlib.Path(__file__).parents[2] / "oblako/images/redshift/proxy/super_nav.py"
_spec = importlib.util.spec_from_file_location("_super_nav", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


@pytest.fixture(autouse=True)
def _clean_columns():
    """Isolate the module-global SUPER_COLUMNS per test."""
    saved = set(_mod.SUPER_COLUMNS)
    _mod.SUPER_COLUMNS.clear()
    yield
    _mod.SUPER_COLUMNS.clear()
    _mod.SUPER_COLUMNS.update(saved)


# --- learning SUPER columns from DDL ---------------------------------------
def test_record_super_columns_from_create():
    _mod.record_super_columns("CREATE TABLE t (id int, doc SUPER, info super)")
    assert _mod.SUPER_COLUMNS == {"doc", "info"}


def test_record_ignores_non_ddl():
    _mod.record_super_columns("SELECT super_col FROM t")  # not a CREATE/ALTER
    assert _mod.SUPER_COLUMNS == set()


def test_record_alter_add_column():
    _mod.record_super_columns("ALTER TABLE t ADD COLUMN payload SUPER")
    assert "payload" in _mod.SUPER_COLUMNS


def test_record_ctas_cast_alias():
    # CREATE TABLE AS SELECT ...::super AS data  -> 'data' is a SUPER column
    _mod.record_super_columns("CREATE TABLE s AS SELECT (x)::super AS data FROM t")
    assert "data" in _mod.SUPER_COLUMNS


# --- rewriting navigation ---------------------------------------------------
def test_dot_navigation_to_jsonb_path():
    _mod.SUPER_COLUMNS.add("data")
    assert (
        _mod.rewrite_super_paths("SELECT data.a.b FROM t")
        == "SELECT (data #> ARRAY['a', 'b'])::text AS \"b\" FROM t"
    )


def test_alias_qualified_navigation():
    _mod.SUPER_COLUMNS.add("data")
    assert (
        _mod.rewrite_super_paths("SELECT c.data.customer.name FROM c")
        == "SELECT (c.data #> ARRAY['customer', 'name'])::text AS \"name\" FROM c"
    )


def test_mixed_dot_and_bracket_with_array_index():
    _mod.SUPER_COLUMNS.add("data")
    assert (
        _mod.rewrite_super_paths("SELECT data.items[0].sku FROM t")
        == "SELECT (data #> ARRAY['items', '0', 'sku'])::text AS \"sku\" FROM t"
    )


def test_pure_bracket_navigation_left_for_native_jsonb():
    # data['a']['b'] works natively via jsonb subscripting; the rewrite only
    # handles the dot form, so a pure-bracket chain is left untouched.
    _mod.SUPER_COLUMNS.add("data")
    sql = "SELECT data['a']['b'] FROM t"
    assert _mod.rewrite_super_paths(sql) == sql


def test_bare_super_column_is_untouched():
    # no navigation steps -> the whole SUPER value, left alone
    _mod.SUPER_COLUMNS.add("data")
    assert _mod.rewrite_super_paths("SELECT data FROM t") == "SELECT data FROM t"


def test_non_super_table_column_is_untouched():
    _mod.SUPER_COLUMNS.add("data")
    sql = "SELECT t.name.first FROM t"  # 'name' is not a SUPER column
    assert _mod.rewrite_super_paths(sql) == sql


def test_navigation_inside_string_literal_is_not_rewritten():
    _mod.SUPER_COLUMNS.add("data")
    sql = "SELECT id FROM t WHERE note = 'data.a.b is a path'"
    assert _mod.rewrite_super_paths(sql) == sql


def test_filter_predicate_is_rewritten():
    _mod.SUPER_COLUMNS.add("data")
    out = _mod.rewrite_super_paths("SELECT id FROM t WHERE data.type = 'premium'")
    assert out == "SELECT id FROM t WHERE (data #>> ARRAY['type']) = 'premium'"


def test_noop_when_no_super_columns_known():
    assert _mod.rewrite_super_paths("SELECT data.a FROM t") == "SELECT data.a FROM t"


def test_a_selected_item_is_json_text_as_redshift_sends_super():
    # checked on Redshift Serverless (2026-10): a selected SUPER value is JSON text
    # ("Alice", quotes included) named after the path's last key, data.tags[0] is
    # "tags"; a cast gives the plain value and keeps that name; inside an
    # expression the navigated value is text
    _mod.SUPER_COLUMNS.add("data")
    out = _mod.rewrite_super_paths(
        "SELECT data.name AS who, data.tags[0], data.age::int, upper(data.city) FROM t"
    )
    assert out == (
        "SELECT (data #> ARRAY['name'])::text AS who, "
        "(data #> ARRAY['tags', '0'])::text AS \"tags\", "
        "(data #>> ARRAY['age'])::int AS \"age\", "
        "upper((data #>> ARRAY['city'])) FROM t"
    )


def test_grouped_and_ordered_items_match_the_selected_one():
    _mod.SUPER_COLUMNS.add("data")
    out = _mod.rewrite_super_paths(
        "SELECT data.a, count(*) FROM t GROUP BY data.a ORDER BY data.a DESC"
    )
    item = "(data #> ARRAY['a'])::text"
    assert out == (
        f'SELECT {item} AS "a", count(*) FROM t GROUP BY {item} ORDER BY {item} DESC'
    )
