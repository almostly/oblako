import awswrangler as wr
import pandas as pd

signups = pd.DataFrame(
    {
        "day": ["2026-09-01", "2026-09-01", "2026-09-02"],
        "user": ["u-1", "u-2", "u-4"],
        "plan": ["free", "team", "free"],
    }
)
wr.s3.to_parquet(
    signups,
    path="s3://lake-data/signups/",
    dataset=True,
    database="web",
    table="signups",
    partition_cols=["day"],
)

df = wr.athena.read_sql_query(
    "SELECT plan, count(*) AS n FROM signups"
    " GROUP BY plan ORDER BY plan",
    database="web",
)
print(df)
