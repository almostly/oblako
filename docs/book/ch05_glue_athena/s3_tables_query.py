from athena_query import run_query

sql = (
    "SELECT day, sum(views) AS views"
    ' FROM "s3tablescatalog/analytics"."web"."page_views"'
    " GROUP BY day ORDER BY day"
)
for row in run_query(sql):
    print(row)
