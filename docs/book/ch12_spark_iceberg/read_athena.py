from athena_query import run_query

sql = (
    "SELECT date(placed_at) AS day, count(*) AS orders"
    ' FROM "s3tablescatalog/lakehouse"."sales"."orders"'
    " GROUP BY 1 ORDER BY 1"
)
for row in run_query(sql):
    print(row)
