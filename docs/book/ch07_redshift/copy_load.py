from warehouse import query

query(
    "COPY page_views FROM 's3://wh-staging/page_views/'"
    " IAM_ROLE default FORMAT AS PARQUET"
)
print(
    query("SELECT count(*), min(day), max(day) FROM page_views")
)
