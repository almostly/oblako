from cluster import query

rows = query("""
    SELECT r.name, count(*) AS orders, sum(o.total) AS revenue
    FROM orders o
    JOIN customers c ON c.customer_id = o.customer_id
    JOIN regions r ON r.region = c.region
    GROUP BY r.name
    ORDER BY revenue DESC
    LIMIT 3""")
for row in rows:
    print(*row)
