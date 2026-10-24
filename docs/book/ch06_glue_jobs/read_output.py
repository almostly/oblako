import boto3
import pandas as pd
from pyarrow import fs

# pandas reads and writes S3 through pyarrow, whose S3 client
# ignores the profile's endpoints, so it gets the one boto3 uses
endpoint = boto3.client("s3").meta.endpoint_url
s3_fs = fs.S3FileSystem(endpoint_override=endpoint)

clean = pd.read_parquet(
    "etl-demo/clean/clicks/", filesystem=s3_fs
)
print(clean.sort_values(["day", "user"]).to_string(index=False))
