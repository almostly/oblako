import time

from db import PASSWORD, USER, connect, endpoint, one

COUNT = "SELECT count(*) FROM orders"
ORDERS = """
    CREATE TABLE orders (
        id        bigint PRIMARY KEY,
        customer  text NOT NULL,
        total     numeric(10, 2) NOT NULL,
        placed_at timestamptz NOT NULL DEFAULT now()
    )"""

with connect("shop-primary") as primary:
    primary.execute(
        "CREATE PUBLICATION shop_orders FOR TABLE orders"
    )

source = endpoint("shop-primary")
dsn = (
    f"host={source['Address']} port={source['Port']} "
    f"dbname=shop user={USER} password={PASSWORD}"
)
copying = """
    SELECT bool_or(srsubstate <> 'r') FROM pg_subscription_rel"""
with connect("shop-analytics") as analytics:
    analytics.execute(ORDERS)
    analytics.execute(
        f"CREATE SUBSCRIPTION shop_orders CONNECTION '{dsn}'"
        " PUBLICATION shop_orders"
    )
    while one(analytics, copying):
        time.sleep(0.2)
    print("copied:", one(analytics, COUNT))
