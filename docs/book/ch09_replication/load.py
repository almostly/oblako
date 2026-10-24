from db import connect

with connect("shop-primary") as conn:
    conn.execute("""
        CREATE TABLE orders (
            id        bigint PRIMARY KEY,
            customer  text NOT NULL,
            total     numeric(10, 2) NOT NULL,
            placed_at timestamptz NOT NULL DEFAULT now()
        )""")
    conn.execute("""
        INSERT INTO orders (id, customer, total)
        SELECT g, 'customer-' || (g % 50),
               round((g % 97) * 1.25, 2)
        FROM generate_series(1, 1000) AS g""")
    totals = "SELECT count(*), sum(total) FROM orders"
    print(conn.execute(totals).fetchone())
