from cluster import query

rows = query("""
    SELECT "table", diststyle, sortkey1, tbl_rows, skew_rows
    FROM svv_table_info
    WHERE "table" IN ('customers', 'orders', 'regions')
    ORDER BY "table" """)
for row in rows:
    print(*row)
