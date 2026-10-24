import time

from db import connect, one

with connect("shop-primary") as primary:
    primary.execute(
        "INSERT INTO orders (id, customer, total)"
        " VALUES (1001, 'customer-7', 42.00)"
    )
    lsn = one(primary, "SELECT pg_current_wal_lsn()")

caught_up = "SELECT pg_last_wal_replay_lsn() >= %s::pg_lsn"
with connect("shop-replica") as replica:
    polls = 0
    while not one(replica, caught_up, (lsn,)):
        time.sleep(0.01)
        polls += 1
    row = replica.execute(
        "SELECT customer, total FROM orders WHERE id = 1001"
    ).fetchone()
    print("replayed", lsn, "after", polls, "polls:", row)
