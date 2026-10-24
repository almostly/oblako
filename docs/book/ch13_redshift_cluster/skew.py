from cluster import query

query("""
    CREATE TABLE orders_by_day DISTKEY(order_date) AS
    SELECT * FROM orders""")
rows = query("""
    SELECT "table", diststyle, skew_rows
    FROM svv_table_info
    WHERE "table" IN ('orders', 'orders_by_day')
    ORDER BY "table" """)
for row in rows:
    print(*row)
