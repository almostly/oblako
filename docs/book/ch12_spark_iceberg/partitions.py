from lake import session

spark = session()
spark.sql("""
    SELECT partition.placed_at_day AS day,
           record_count, file_count
    FROM lake.sales.orders.partitions
    ORDER BY day""").show()
