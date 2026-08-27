"""Integration test: redshift-local MPP variant (Citus) shards across workers.

Requires the cluster profile running (``docker compose --profile cluster up``):
a Citus coordinator on 5439 plus two workers. Proves the single-node image's
whole compat layer works on Citus: redshift_connector connects natively (the
proxy presents Redshift's version on the wire while the engine keeps its real
version so Citus loads), tables distribute across workers, and plpython UDFs run
per-shard. Skips cleanly when the single-node ``redshift`` service is on 5439
instead (no Citus), so it only runs against an actual cluster.
"""

import psycopg2
import pytest

RS = dict(
    host="localhost", port=5439, user="oblako", password="oblako", dbname="oblako"
)


def _active_worker_count() -> int:
    """Active Citus nodes, or 0 if 5439 isn't a cluster (single-node / absent)."""
    try:
        c = psycopg2.connect(sslmode="require", connect_timeout=3, **RS)
        try:
            cur = c.cursor()
            cur.execute("SELECT count(*) FROM pg_dist_node WHERE isactive")
            return cur.fetchone()[0]
        finally:
            c.close()
    except Exception:
        return 0


# a cluster is coordinator + >= 2 workers
cluster = pytest.mark.skipif(
    _active_worker_count() < 3, reason="redshift MPP cluster not running on 5439"
)
TABLE = "cluster_events"


@pytest.fixture
def conn():
    # redshift_connector (not psycopg2) so the native handshake on Citus is
    # exercised: the proxy must present a Redshift version or the driver refuses.
    import redshift_connector

    c = redshift_connector.connect(
        host="localhost",
        port=5439,
        database="oblako",
        user="oblako",
        password="oblako",
        ssl=False,
    )
    c.autocommit = True
    cur = c.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    cur.execute(f"CREATE TABLE {TABLE} (id bigint, user_id int, amount numeric)")
    cur.execute(f"SELECT create_distributed_table('{TABLE}', 'user_id')")
    cur.execute(
        f"INSERT INTO {TABLE} SELECT g, g%1000, (g%100)*1.5 "
        f"FROM generate_series(1, 200000) g"
    )
    yield cur
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    c.close()


@cluster
def test_native_handshake_and_topology(conn):
    # redshift_connector connected at all (fixture) -> native handshake on Citus.
    conn.execute("SELECT count(*) FROM pg_dist_node WHERE isactive")
    assert conn.fetchone()[0] >= 3  # coordinator + 2 workers


@cluster
def test_table_shards_across_workers(conn):
    conn.execute(
        f"SELECT nodename, count(*) FROM citus_shards "
        f"WHERE table_name='{TABLE}'::regclass GROUP BY nodename"
    )
    placement = dict(conn.fetchall())
    assert len(placement) >= 2, f"shards not spread across workers: {placement}"
    assert sum(placement.values()) > 0


@cluster
def test_distributed_aggregation_correct(conn):
    conn.execute(
        f"SELECT count(*), count(distinct user_id), round(sum(amount)) FROM {TABLE}"
    )
    total, users, revenue = conn.fetchone()
    assert total == 200000
    assert users == 1000


@cluster
def test_plpython_udf_over_distributed_table(conn):
    # plpython UDF, distributed to the workers, applied inside a distributed scan
    conn.execute(
        "CREATE OR REPLACE FUNCTION bump(x int) RETURNS int "
        "LANGUAGE plpython3u AS $$ return x + 1 $$"
    )
    conn.execute("SELECT create_distributed_function('bump(int)')")
    conn.execute(f"SELECT count(*) FROM {TABLE} WHERE bump(user_id) > 0")
    assert conn.fetchone()[0] == 200000


def _partmethod(conn, table: str, timeout: float = 8.0) -> str:
    """Poll pg_dist_partition; return 'distributed' / 'reference' / 'local'.

    Auto-distribution runs on a side connection just after CREATE commits, so give
    it a moment to land.
    """
    import time

    deadline = time.time() + timeout
    while True:
        conn.execute(
            "SELECT partmethod FROM pg_dist_partition "
            f"WHERE logicalrelid = '{table}'::regclass"
        )
        row = conn.fetchone()
        method = {"h": "distributed", "n": "reference"}.get(row[0] if row else None)
        if method or time.time() > deadline:
            return method or "local"
        time.sleep(0.5)


def _poll_count(conn, sql: str, timeout: float = 8.0) -> int:
    """Poll a scalar ``count(*)`` query until it is > 0, or return it on timeout.

    Post-distribution work (SORTKEY index, etc.) lands on the same side connection
    just after the DDL commits, and can trail the pg_dist_partition update by a
    moment, so a single immediate read races it.
    """
    import time

    deadline = time.time() + timeout
    while True:
        conn.execute(sql)
        n = conn.fetchone()[0]
        if n > 0 or time.time() > deadline:
            return n
        time.sleep(0.5)


@cluster
def test_auto_distribute_distkey(conn):
    # increment #2: plain Redshift DDL (no create_distributed_table) -> the proxy
    # turns DISTKEY into a distributed table, sharded across the workers.
    conn.execute("DROP TABLE IF EXISTS auto_ev")
    conn.execute(
        "CREATE TABLE auto_ev (id bigint, user_id int, amount numeric) "
        "DISTSTYLE KEY DISTKEY(user_id) SORTKEY(id)"
    )
    assert _partmethod(conn, "auto_ev") == "distributed"
    conn.execute(
        "SELECT count(distinct nodename) FROM citus_shards "
        "WHERE table_name = 'auto_ev'::regclass"
    )
    assert conn.fetchone()[0] >= 2  # shards spread across the workers
    conn.execute("DROP TABLE auto_ev")


@cluster
def test_auto_distribute_diststyle_all_is_reference(conn):
    conn.execute("DROP TABLE IF EXISTS auto_dim")
    conn.execute("CREATE TABLE auto_dim (id int, name text) DISTSTYLE ALL")
    assert _partmethod(conn, "auto_dim") == "reference"
    conn.execute("DROP TABLE auto_dim")


@cluster
def test_no_distkey_stays_local(conn):
    conn.execute("DROP TABLE IF EXISTS auto_loc")
    conn.execute("CREATE TABLE auto_loc (id int, x int)")
    # a plain table gets no auto-distribution; it stays local on the coordinator
    assert _partmethod(conn, "auto_loc", timeout=3.0) == "local"
    conn.execute("DROP TABLE auto_loc")


@cluster
def test_sortkey_becomes_an_index(conn):
    # SORTKEY on a distributed table -> a btree index on those columns
    conn.execute("DROP TABLE IF EXISTS auto_sk")
    conn.execute(
        "CREATE TABLE auto_sk (id bigint, user_id int, amount numeric) "
        "DISTSTYLE KEY DISTKEY(user_id) SORTKEY(id)"
    )
    assert _partmethod(conn, "auto_sk") == "distributed"
    # the SORTKEY btree index is created just after distribution on the side
    # connection, so poll for it rather than reading once and racing the creation
    n = _poll_count(conn, "SELECT count(*) FROM pg_indexes WHERE tablename = 'auto_sk'")
    assert n >= 1  # the SORTKEY index
    conn.execute("DROP TABLE auto_sk")


@cluster
def test_auto_distribute_in_transaction():
    # dbt wraps DDL in a transaction: the distribution must fire on COMMIT, and
    # the rows written in the same transaction must survive the distribution.
    import time

    import redshift_connector

    c = redshift_connector.connect(
        host="localhost",
        port=5439,
        database="oblako",
        user="oblako",
        password="oblako",
        ssl=False,
    )
    try:
        cur = c.cursor()
        cur.execute("DROP TABLE IF EXISTS txn_tbl")
        c.commit()
        cur.execute("CREATE TABLE txn_tbl (id bigint, k int, v numeric) DISTKEY(k)")
        cur.execute(
            "INSERT INTO txn_tbl SELECT g, g%200, g*1.0 FROM generate_series(1,50000) g"
        )
        c.commit()  # distribution fires here
        time.sleep(3)
        cur.execute(
            "SELECT partmethod FROM pg_dist_partition WHERE logicalrelid='txn_tbl'::regclass"
        )
        row = cur.fetchone()
        assert row and row[0] == "h", "not distributed after COMMIT"
        cur.execute("SELECT count(*) FROM txn_tbl")
        assert cur.fetchone()[0] == 50000  # rows preserved through distribution
        cur.execute("DROP TABLE txn_tbl")
        c.commit()
    finally:
        c.close()
