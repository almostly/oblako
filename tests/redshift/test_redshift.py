"""Integration tests for Redshift (requires: docker compose up redshift).

Backed by oblako/redshift on port 5439.
"""

import psycopg2
import pytest

RS_CONFIG = dict(
    host="localhost", port=5439, user="oblako", password="oblako", dbname="oblako"
)


@pytest.fixture
def conn():
    c = psycopg2.connect(**RS_CONFIG)
    c.autocommit = True
    yield c
    c.close()


@pytest.fixture
def cursor(conn):
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS test_scores")
    cur.execute("""
        CREATE TABLE test_scores (
            customer_id TEXT PRIMARY KEY,
            score FLOAT,
            decision TEXT
        )
    """)
    yield cur
    cur.execute("DROP TABLE IF EXISTS test_scores")
    cur.close()


def test_insert_and_select(cursor):
    cursor.execute(
        "INSERT INTO test_scores VALUES (%s, %s, %s)",
        ("C001", 0.85, "APPROVED"),
    )
    cursor.execute("SELECT * FROM test_scores WHERE customer_id = 'C001'")
    row = cursor.fetchone()
    assert row == ("C001", 0.85, "APPROVED")


def test_batch_insert(cursor):
    rows = [
        (f"C{i:03d}", i / 100, "APPROVED" if i > 50 else "DECLINED") for i in range(100)
    ]
    cursor.executemany("INSERT INTO test_scores VALUES (%s, %s, %s)", rows)
    cursor.execute("SELECT COUNT(*) FROM test_scores")
    assert cursor.fetchone()[0] == 100


def test_aggregation(cursor):
    cursor.executemany(
        "INSERT INTO test_scores VALUES (%s, %s, %s)",
        [
            ("C001", 0.9, "APPROVED"),
            ("C002", 0.3, "DECLINED"),
            ("C003", 0.7, "APPROVED"),
        ],
    )
    cursor.execute("SELECT AVG(score) FROM test_scores WHERE decision = 'APPROVED'")
    avg = cursor.fetchone()[0]
    assert abs(avg - 0.8) < 0.01


def test_query_group(cursor):
    """the Redshift engine accepts Redshift's `SET query_group`."""
    cursor.execute("SET query_group TO 'batch_scoring'")
    cursor.execute("SHOW query_group")
    assert cursor.fetchone()[0] == "batch_scoring"


def test_stl_system_table(cursor):
    """the Redshift engine ships Redshift STL/STV system tables (e.g. stl_scan)."""
    cursor.execute("SELECT COUNT(*) FROM stl_scan")
    assert cursor.fetchone()[0] > 0


def test_redshift_json_udf(cursor):
    """the Redshift engine provides Redshift JSON UDFs not present in stock PostgreSQL."""
    cursor.execute("SELECT json_array_length(%s)", ("[1, 2, 3]",))
    assert cursor.fetchone()[0] == 3
