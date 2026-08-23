"""Integration tests for Redshift PIVOT / UNPIVOT (rewritten in the wire proxy).

Requires the engine (docker compose up redshift). The proxy rewrites PIVOT into
conditional aggregation and UNPIVOT into a LATERAL VALUES join, for a subquery
source. Override OBLAKO_TEST_RS_PORT to run against an isolated stack.
"""

import os

import psycopg2
import pytest

RS_PORT = int(os.environ.get("OBLAKO_TEST_RS_PORT", "5439"))
RS = dict(
    host="localhost", port=RS_PORT, user="oblako", password="oblako", dbname="oblako"
)


def _pivot_present() -> bool:
    """True if the proxy rewrites PIVOT (so it reaches the engine as standard SQL)."""
    try:
        c = psycopg2.connect(connect_timeout=3, **RS)
        c.autocommit = True
        try:
            cur = c.cursor()
            cur.execute(
                "SELECT x FROM (SELECT 1 AS a, 'k' AS b, 5 AS c) s "
                "PIVOT (SUM(c) FOR b IN ('k' AS x))"
            )
            return cur.fetchone()[0] == 5
        except Exception:
            return False
        finally:
            c.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pivot_present(), reason="redshift image without the PIVOT/UNPIVOT rewrite"
)


@pytest.fixture
def cur():
    c = psycopg2.connect(**RS)
    c.autocommit = True
    yield c.cursor()
    c.close()


def test_pivot_conditional_aggregation(cur):
    cur.execute("DROP TABLE IF EXISTS pv_emp")
    cur.execute("CREATE TABLE pv_emp (dept text, gender text, salary int)")
    cur.execute(
        "INSERT INTO pv_emp VALUES "
        "('eng','M',100),('eng','F',200),('sales','M',300),"
        "('sales','F',400),('eng','M',150)"
    )
    cur.execute(
        "SELECT dept, male, female FROM ("
        "  SELECT dept, gender, salary FROM pv_emp) AS s "
        "PIVOT (AVG(salary) FOR gender IN ('M' AS male, 'F' AS female)) "
        "ORDER BY dept"
    )
    rows = [(d, float(m), float(f)) for d, m, f in cur.fetchall()]
    assert rows == [("eng", 125.0, 200.0), ("sales", 300.0, 400.0)]
    cur.execute("DROP TABLE pv_emp")


def test_unpivot_lateral_values_excludes_nulls(cur):
    cur.execute("DROP TABLE IF EXISTS pv_sales")
    cur.execute("CREATE TABLE pv_sales (id int, q1 int, q2 int, q3 int)")
    cur.execute("INSERT INTO pv_sales VALUES (1,10,20,NULL),(2,30,NULL,50)")
    cur.execute(
        "SELECT id, quarter, amount FROM ("
        "  SELECT id, q1, q2, q3 FROM pv_sales) AS s "
        "UNPIVOT (amount FOR quarter IN (q1, q2, q3)) "
        "ORDER BY id, quarter"
    )
    assert cur.fetchall() == [
        (1, "q1", 10),
        (1, "q2", 20),
        (2, "q1", 30),
        (2, "q3", 50),
    ]
    cur.execute("DROP TABLE pv_sales")
