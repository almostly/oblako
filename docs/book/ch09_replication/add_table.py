import time

from db import connect, one

CUSTOMERS = """
    CREATE TABLE customers (
        name    text PRIMARY KEY,
        segment text NOT NULL
    )"""

with connect("shop-primary") as primary:
    primary.execute(CUSTOMERS)
    primary.execute("""
        INSERT INTO customers
        SELECT 'customer-' || g,
               CASE WHEN g % 5 = 0 THEN 'wholesale'
                    ELSE 'retail' END
        FROM generate_series(0, 49) AS g""")
    primary.execute(
        "ALTER PUBLICATION shop_orders ADD TABLE customers"
    )

copying = """
    SELECT bool_or(srsubstate <> 'r') FROM pg_subscription_rel"""
with connect("shop-analytics") as analytics:
    analytics.execute(CUSTOMERS)
    analytics.execute(
        "ALTER SUBSCRIPTION shop_orders REFRESH PUBLICATION"
    )
    while one(analytics, copying):
        time.sleep(0.2)
    segments = analytics.execute("""
        SELECT segment, count(*) FROM customers
        GROUP BY segment ORDER BY segment""")
    print(segments.fetchall())
