"""Integration tests: redshift-local with the psycopg 3 driver.

Requires the engine (docker compose up redshift). The other Redshift tests use
psycopg2, which interpolates parameters client-side and sends one simple-protocol
query. psycopg 3 binds parameters server-side (Parse/Bind/Execute), prepares a
statement after a few executions, and can pipeline, so the wire proxy's rewrites
(physical DDL, SUPER dot navigation) have to hold on the extended protocol too.

Override the port with OBLAKO_TEST_RS_PORT to run against an isolated stack.
"""

import datetime
import os

import pytest

psycopg = pytest.importorskip("psycopg")

RS_PORT = int(os.environ.get("OBLAKO_TEST_RS_PORT", "5439"))
RS = dict(
    host="localhost", port=RS_PORT, user="oblako", password="oblako", dbname="oblako"
)


def _engine_up() -> bool:
    try:
        psycopg.connect(connect_timeout=3, **RS).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _engine_up(), reason="redshift engine not running")


@pytest.fixture
def conn():
    with psycopg.connect(autocommit=True, **RS) as c:
        c.execute("DROP TABLE IF EXISTS pg3_loans")
        c.execute(
            "CREATE TABLE pg3_loans (id int ENCODE az64, name varchar(max), d date) "
            "DISTSTYLE KEY DISTKEY(id) COMPOUND SORTKEY(id, d)"
        )
        yield c
        c.execute("DROP TABLE IF EXISTS pg3_loans")


def test_tls_and_redshift_server_version():
    with psycopg.connect(sslmode="require", **RS) as c:
        assert c.info.pgconn.ssl_in_use
        assert c.info.parameter_status("server_version") == "8.0.2"


def test_server_side_parameters_with_physical_ddl(conn):
    conn.execute("INSERT INTO pg3_loans VALUES (%s, %s, %s)", (1, "a", "2026-01-31"))
    conn.cursor().executemany(
        "INSERT INTO pg3_loans VALUES (%s, %s, %s)",
        [(i, f"n{i}", "2026-02-01") for i in range(2, 6)],
    )
    assert conn.execute(
        "SELECT count(*) FROM pg3_loans WHERE id > %s", (1,)
    ).fetchone() == (4,)


def test_prepared_statements(conn):
    # psycopg 3 prepares a query server-side after 5 executions (prepare_threshold)
    values = [conn.execute("SELECT %s::int", (i,)).fetchone()[0] for i in range(8)]
    assert values == list(range(8))
    assert conn.execute("SELECT %s::int * 2", (21,), prepare=True).fetchone() == (42,)


def test_pipeline_mode(conn):
    with conn.pipeline():
        cursors = [conn.execute("SELECT %s::int", (i,)) for i in range(3)]
    assert [c.fetchone()[0] for c in cursors] == [0, 1, 2]


def test_redshift_functions_with_parameters(conn):
    added, days = conn.execute(
        "SELECT dateadd('month', 1, %s::date), "
        "datediff('day', %s::date, '2026-03-01'::date)",
        ("2026-01-31", "2026-01-01"),
    ).fetchone()
    assert added == datetime.datetime(2026, 2, 28)  # month-end clamping
    assert days == 59


def test_super_dot_navigation_with_parameters(conn):
    conn.execute("DROP TABLE IF EXISTS pg3_events")
    try:
        conn.execute("CREATE TABLE pg3_events (data super)")
        conn.execute(
            "INSERT INTO pg3_events VALUES (json_parse(%s))",
            ('{"a": {"b": 7}, "l": [1, 2]}',),
        )
        row = conn.execute(
            "SELECT e.data.a.b, e.data['l'][1] FROM pg3_events e WHERE e.data.a.b = %s",
            ("7",),
        ).fetchone()
        assert row == ("7", 2)
    finally:
        conn.execute("DROP TABLE IF EXISTS pg3_events")


def test_copy_protocol(conn):
    with conn.cursor().copy("COPY pg3_loans (id, name, d) FROM STDIN") as copy:
        copy.write_row((10, "copied", "2026-03-01"))
    assert conn.execute("SELECT name FROM pg3_loans WHERE id = 10").fetchone() == (
        "copied",
    )
    with conn.cursor().copy("COPY (SELECT id FROM pg3_loans) TO STDOUT") as copy:
        assert list(copy.rows()) == [("10",)]


def test_transactions_and_named_cursor():
    with psycopg.connect(**RS) as c:
        c.execute("DROP TABLE IF EXISTS pg3_tx")
        c.execute("CREATE TABLE pg3_tx (id int)")
        c.commit()
        with c.transaction():
            c.execute("INSERT INTO pg3_tx VALUES (%s)", (1,))
        with pytest.raises(RuntimeError), c.transaction():
            c.execute("INSERT INTO pg3_tx VALUES (%s)", (2,))
            raise RuntimeError
        with c.cursor(name="pg3_named") as cur:
            cur.execute("SELECT id FROM pg3_tx ORDER BY id")
            assert cur.fetchall() == [(1,)]
        c.execute("DROP TABLE pg3_tx")
        c.commit()
