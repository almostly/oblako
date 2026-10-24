from lake import session

spark = session()
spark.sql(
    "ALTER TABLE lake.sales.orders ADD COLUMN channel string"
)
spark.sql("""
    INSERT INTO lake.sales.orders
    VALUES (9001, 'customer-1', 20.00,
            timestamp'2026-10-07 18:00:00', 'web')""")
spark.sql("""
    SELECT channel, count(*) AS orders
    FROM lake.sales.orders GROUP BY channel""").show()
