from athena_query import run_query

for row in run_query(
    "SELECT page, count(*) AS clicks, avg(ms) AS avg_ms"
    " FROM clicks WHERE day >= '2026-09-02'"
    " GROUP BY page ORDER BY clicks DESC, page"
):
    print(row)
