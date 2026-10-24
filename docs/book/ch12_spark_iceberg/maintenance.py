from lake import session

spark = session()


def files_and_snapshots():
    counts = [
        spark.sql(f"SELECT count(*) FROM lake.sales.orders.{t}")
        for t in ("files", "snapshots")
    ]
    return tuple(c.first()[0] for c in counts)


print("before (files, snapshots):", files_and_snapshots())
compacted = spark.sql("""
    CALL lake.system.rewrite_data_files(
        table => 'sales.orders',
        options => map('min-input-files', '2'))""").first()
rewritten = compacted.rewritten_data_files_count
added = compacted.added_data_files_count
print("rewrote", rewritten, "files into", added)
expired = spark.sql("""
    CALL lake.system.expire_snapshots(
        table => 'sales.orders',
        older_than => current_timestamp(),
        retain_last => 1)""").first()
deleted = expired.deleted_data_files_count
print("deleted", deleted, "unreferenced files")
print("after (files, snapshots):", files_and_snapshots())
