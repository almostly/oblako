import time

from db import connect, one

confirmed = """
    SELECT confirmed_flush_lsn >= %s::pg_lsn
    FROM pg_replication_slots WHERE slot_name = 'shop_orders'"""

with connect("shop-primary") as primary:
    primary.execute(
        "INSERT INTO orders (id, customer, total)"
        " VALUES (2001, 'customer-3', 19.99)"
    )
    primary.execute(
        "UPDATE orders SET total = total * 2 WHERE id = 1"
    )
    primary.execute("DELETE FROM orders WHERE id = 2")
    lsn = one(primary, "SELECT pg_current_wal_lsn()")
    while not one(primary, confirmed, (lsn,)):
        time.sleep(0.1)

with connect("shop-analytics") as analytics:
    rows = analytics.execute(
        "SELECT id, total FROM orders"
        " WHERE id IN (1, 2, 2001) ORDER BY id"
    )
    print(rows.fetchall())
    count = one(analytics, "SELECT count(*) FROM orders")
    print("orders:", count)
