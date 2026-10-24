import boto3
import pandas as pd
from pyarrow import fs

from warehouse import query

query(
    "UNLOAD ('SELECT day, count(*) AS views"
    " FROM page_views GROUP BY day')"
    " TO 's3://wh-staging/exports/daily_' IAM_ROLE default"
    " FORMAT AS PARQUET ALLOWOVERWRITE"
)
# pandas reads and writes S3 through pyarrow, whose S3 client
# ignores the profile's endpoints, so it gets the one boto3 uses
endpoint = boto3.client("s3").meta.endpoint_url
s3_fs = fs.S3FileSystem(endpoint_override=endpoint)
daily = pd.read_parquet("wh-staging/exports/", filesystem=s3_fs)
print(daily.sort_values("day").to_string(index=False))
