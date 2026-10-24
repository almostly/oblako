from lake import session

spark = session()
spark.sql("""
    DELETE FROM lake.sales.orders
    WHERE placed_at < timestamp'2026-10-02 00:00:00'""")
spark.sql("""
    SELECT operation,
           summary['deleted-data-files'] AS files_removed,
           summary['added-data-files'] AS files_added
    FROM lake.sales.orders.snapshots
    ORDER BY committed_at""").show()
