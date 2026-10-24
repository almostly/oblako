from lake import session

spark = session()
spark.sql("CREATE NAMESPACE IF NOT EXISTS lake.sales")
spark.sql("""
    CREATE TABLE lake.sales.orders (
        order_id  bigint,
        customer  string,
        total     decimal(10, 2),
        placed_at timestamp)
    USING iceberg
    PARTITIONED BY (days(placed_at))""")

# 7,000 orders, 1,000 a day from 1 October 2026
spark.sql("""
    INSERT INTO lake.sales.orders
    SELECT id,
           concat('customer-', id % 50),
           CAST((id % 97) * 1.25 AS decimal(10, 2)),
           timestamp'2026-10-01 00:00:00'
             + make_interval(0, 0, 0, CAST(id DIV 1000 AS int),
                             0, 0, CAST(id % 1000 * 60 AS int))
    FROM range(7000)""")
spark.sql("""
    SELECT date(placed_at) AS day, count(*) AS orders
    FROM lake.sales.orders GROUP BY 1 ORDER BY 1""").show()
