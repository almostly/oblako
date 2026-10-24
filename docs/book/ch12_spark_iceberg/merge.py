from lake import session

spark = session()
# corrected totals for 100 existing orders, and 50 late orders
spark.sql("""
    SELECT id AS order_id,
           concat('customer-', id % 50) AS customer,
           CAST(id % 97 * 1.25 + 5 AS decimal(10, 2)) AS total,
           timestamp'2026-10-07 12:00:00' AS placed_at
    FROM range(6900, 7050)""").createOrReplaceTempView("updates")
spark.sql("""
    MERGE INTO lake.sales.orders AS o
    USING updates AS u
    ON o.order_id = u.order_id
    WHEN MATCHED THEN UPDATE SET o.total = u.total
    WHEN NOT MATCHED THEN INSERT *""")
spark.sql("""
    SELECT count(*) AS orders, sum(total) AS revenue
    FROM lake.sales.orders""").show()
