import psycopg

from db import connect, one

RECOVERY = "SELECT pg_is_in_recovery()"

with connect("shop-primary") as primary:
    standbys = primary.execute(
        "SELECT state, sync_state FROM pg_stat_replication"
    )
    print("primary sends to:", standbys.fetchall())

with connect("shop-replica") as replica:
    print("in recovery:", one(replica, RECOVERY))
    print("orders:", one(replica, "SELECT count(*) FROM orders"))
    try:
        replica.execute("DELETE FROM orders")
    except psycopg.errors.ReadOnlySqlTransaction as e:
        print("refused:", e)
