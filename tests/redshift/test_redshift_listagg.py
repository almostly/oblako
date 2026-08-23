"""Integration tests: LISTAGG (proxy rewrite) and the JSON extract functions.

Requires the engine (docker compose up redshift). LISTAGG is rewritten to
PostgreSQL's ``string_agg`` in the wire proxy; the JSON functions are native
SQL wrappers over jsonb in the engine. Override OBLAKO_TEST_RS_PORT to run
against an isolated stack.
"""

import os

import psycopg2
import pytest

RS_PORT = int(os.environ.get("OBLAKO_TEST_RS_PORT", "5439"))
RS = dict(
    host="localhost", port=RS_PORT, user="oblako", password="oblako", dbname="oblako"
)


def _listagg_present() -> bool:
    """True if the proxy rewrites LISTAGG (so it reaches the engine as string_agg)."""
    try:
        c = psycopg2.connect(connect_timeout=3, **RS)
        c.autocommit = True
        try:
            cur = c.cursor()
            cur.execute("SELECT LISTAGG(x, ',') FROM (SELECT 'a' AS x) s")
            return cur.fetchone()[0] == "a"
        except Exception:
            return False
        finally:
            c.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _listagg_present(), reason="redshift image without the LISTAGG rewrite"
)


@pytest.fixture
def cur():
    c = psycopg2.connect(**RS)
    c.autocommit = True
    yield c.cursor()
    c.close()


def test_listagg_within_group_ordered(cur):
    cur.execute("DROP TABLE IF EXISTS la_staff")
    cur.execute("CREATE TABLE la_staff (dept text, emp text)")
    cur.execute(
        "INSERT INTO la_staff VALUES "
        "('eng','carol'),('eng','alice'),('eng','bob'),('sales','dave')"
    )
    cur.execute(
        "SELECT dept, LISTAGG(emp, ', ') WITHIN GROUP (ORDER BY emp) "
        "FROM la_staff GROUP BY dept ORDER BY dept"
    )
    assert cur.fetchall() == [("eng", "alice, bob, carol"), ("sales", "dave")]
    cur.execute("DROP TABLE la_staff")


def test_listagg_distinct(cur):
    cur.execute("DROP TABLE IF EXISTS la_d")
    cur.execute("CREATE TABLE la_d (v text)")
    cur.execute("INSERT INTO la_d VALUES ('b'),('a'),('b'),('a')")
    cur.execute("SELECT LISTAGG(DISTINCT v, '|') WITHIN GROUP (ORDER BY v) FROM la_d")
    assert cur.fetchone()[0] == "a|b"
    cur.execute("DROP TABLE la_d")


def test_json_extract_functions(cur):
    cur.execute(
        "SELECT json_extract_path_text('{\"a\":{\"b\":\"hi\"}}', 'a', 'b'), "
        "json_extract_path_text('{\"a\":1}', 'missing'), "
        'json_extract_array_element_text(\'["x","y","z"]\', 1), '
        "json_array_length('[1,2,3,4]')"
    )
    assert cur.fetchone() == ("hi", "", "y", 4)
