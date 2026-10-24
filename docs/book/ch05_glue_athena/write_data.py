import boto3
import pandas as pd
from pyarrow import fs

s3 = boto3.client("s3")
s3.create_bucket(Bucket="lake-data")
# pandas reads and writes S3 through pyarrow, whose S3 client
# ignores the profile's endpoints, so it gets the one boto3 uses
s3_fs = fs.S3FileSystem(endpoint_override=s3.meta.endpoint_url)
clicks = pd.DataFrame(
    {
        "day": ["2026-09-01"] * 3
        + ["2026-09-02"] * 2
        + ["2026-09-03"] * 3,
        "user": [
            "u-1",
            "u-2",
            "u-3",
            "u-1",
            "u-4",
            "u-2",
            "u-3",
            "u-5",
        ],
        "page": [
            "/",
            "/pricing",
            "/",
            "/docs",
            "/",
            "/docs",
            "/",
            "/pricing",
        ],
        "ms": [820, 1430, 610, 2210, 450, 1980, 700, 1250],
    }
)
clicks.to_parquet(
    "lake-data/clicks/",
    partition_cols=["day"],
    filesystem=s3_fs,
)
print(len(clicks), "rows")
