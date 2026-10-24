import boto3
from store import open_store
from warehouse import run

open_store().teardown()
run("DROP TABLE IF EXISTS customer_stats")
s3 = boto3.client("s3")
listing = s3.list_objects_v2(Bucket="feature-staging")
for obj in listing.get("Contents", []):
    s3.delete_object(Bucket="feature-staging", Key=obj["Key"])
s3.delete_bucket(Bucket="feature-staging")
