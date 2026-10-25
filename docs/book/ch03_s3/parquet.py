import boto3
import pandas as pd
from pyarrow import fs

# pandas reads and writes S3 through pyarrow, whose S3 client
# ignores the profile's endpoints, so it gets the one boto3 uses
endpoint = boto3.client("s3").meta.endpoint_url
s3_fs = fs.S3FileSystem(endpoint_override=endpoint)

clicks = pd.DataFrame(
    {
        "day": [
            "2026-09-01",
            "2026-09-01",
            "2026-09-02",
            "2026-09-03",
        ],
        "user": ["u-1", "u-2", "u-1", "u-3"],
        "page": ["/", "/pricing", "/docs", "/"],
    }
)
clicks.to_parquet(
    "events/clicks/",
    partition_cols=["day"],
    filesystem=s3_fs,
)

one_day = pd.read_parquet(
    "events/clicks/",
    filters=[("day", "=", "2026-09-02")],
    filesystem=s3_fs,
)
print(one_day)
