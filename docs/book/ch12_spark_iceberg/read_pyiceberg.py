from catalog import open_catalog

table = open_catalog().load_table("sales.orders")
orders = table.scan(row_filter="channel == 'web'").to_arrow()
print(orders.num_rows, "web orders")
print(orders.select(["order_id", "total"]).to_pylist())
