from db import connect

SLOTS = """
    SELECT slot_name, slot_type, active
    FROM pg_replication_slots"""

with connect("shop-primary") as primary:
    print("before:", primary.execute(SLOTS).fetchall())

with connect("shop-analytics") as analytics:
    analytics.execute("DROP SUBSCRIPTION shop_orders")

with connect("shop-primary") as primary:
    print("after:", primary.execute(SLOTS).fetchall())
    primary.execute("DROP PUBLICATION shop_orders")
