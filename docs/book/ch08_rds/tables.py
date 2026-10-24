import psycopg

with psycopg.connect(autocommit=True) as conn:
    conn.execute("DROP TABLE IF EXISTS products")
    conn.execute(
        "CREATE TABLE products ("
        " sku text PRIMARY KEY,"
        " name text NOT NULL,"
        " price numeric(10, 2) NOT NULL)"
    )
    row = conn.execute(
        "INSERT INTO products VALUES (%s, %s, %s)"
        " RETURNING sku, price",
        ("TEA-01", "Green tea", 4.50),
    ).fetchone()
    print("inserted:", row)

    upsert = (
        "INSERT INTO products VALUES (%s, %s, %s)"
        " ON CONFLICT (sku) DO UPDATE SET price = EXCLUDED.price"
    )
    conn.execute(upsert, ("TEA-01", "Green tea", 4.95))

    with conn.transaction():
        conn.execute(upsert, ("MUG-01", "Mug", 12.00))
        conn.execute(upsert, ("POT-01", "Teapot", 29.00))

    rows = conn.execute(
        "SELECT sku, price FROM products ORDER BY sku"
    )
    for row in rows:
        print(row)
