import boto3
import pandas as pd
from pyarrow import fs

s3 = boto3.client("s3")
s3.create_bucket(Bucket="wh-staging")
# pandas reads and writes S3 through pyarrow, whose S3 client
# ignores the profile's endpoints, so it gets the one boto3 uses
s3_fs = fs.S3FileSystem(endpoint_override=s3.meta.endpoint_url)
days = [
    "2026-09-01",
    "2026-09-01",
    "2026-09-02",
    "2026-09-02",
    "2026-09-03",
    "2026-09-03",
]
views = pd.DataFrame(
    {
        "day": pd.to_datetime(days).date,
        "user_id": ["u-1", "u-2", "u-1", "u-3", "u-2", "u-3"],
        "page": [
            "/",
            "/pricing",
            "/docs",
            "/",
            "/docs",
            "/pricing",
        ],
        "load_ms": [821, 1430, 2210, 450, 1980, 700],
    }
)
views.to_parquet(
    "wh-staging/page_views/part-0.parquet",
    index=False,
    filesystem=s3_fs,
)
print(len(views), "rows staged")
