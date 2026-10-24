import psycopg

pages = [
    ("pricing", "Plans and prices", "[0.9, 0.1, 0.0]"),
    ("quickstart", "Install and first run", "[0.1, 0.9, 0.2]"),
    ("api", "API reference", "[0.0, 0.8, 0.6]"),
]
with psycopg.connect(autocommit=True) as conn:
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute("DROP TABLE IF EXISTS doc_pages")
    conn.execute(
        "CREATE TABLE doc_pages ("
        " slug text PRIMARY KEY, title text, embedding vector(3))"
    )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO doc_pages VALUES (%s, %s, %s)", pages
        )
    conn.execute(
        "CREATE INDEX ON doc_pages"
        " USING hnsw (embedding vector_cosine_ops)"
    )

    query = "[0.1, 1.0, 0.3]"
    nearest = conn.execute(
        "SELECT slug,"
        " round((embedding <=> %s::vector)::numeric, 4)"
        " FROM doc_pages"
        " ORDER BY embedding <=> %s::vector LIMIT 2",
        (query, query),
    ).fetchall()
    print(nearest)
