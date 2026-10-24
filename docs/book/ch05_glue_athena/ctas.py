from athena_query import run_query

run_query(
    "CREATE TABLE daily_pages"
    " WITH (format = 'PARQUET',"
    " external_location = 's3://lake-data/daily_pages/')"
    " AS SELECT day, page, count(*) AS clicks"
    " FROM clicks GROUP BY day, page"
)
rows = run_query("SELECT * FROM daily_pages ORDER BY day, page")
for row in rows:
    print(row)
