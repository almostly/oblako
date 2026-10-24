from lake import session

spark = session()
first = spark.sql("""
    SELECT snapshot_id FROM lake.sales.orders.snapshots
    ORDER BY committed_at LIMIT 1""").first()[0]
count = "SELECT count(*) FROM lake.sales.orders"
print("now", spark.sql(count).first()[0])
then = spark.sql(f"{count} VERSION AS OF {first}").first()[0]
print("first snapshot", then)
