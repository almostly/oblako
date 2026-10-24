from athena_query import run_query

rows = run_query("""
    SELECT page, count(*) AS clicks,
           count(DISTINCT "user") AS users
    FROM stream_clicks
    GROUP BY page
    ORDER BY clicks DESC""")
for row in rows:
    print(row)
