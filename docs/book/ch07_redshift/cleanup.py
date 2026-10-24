import boto3

from warehouse import query

query("DROP TABLE IF EXISTS page_views")
query("DROP TABLE IF EXISTS raw_events")
s3 = boto3.client("s3")
listing = s3.list_objects_v2(Bucket="wh-staging")
for obj in listing.get("Contents", []):
    s3.delete_object(Bucket="wh-staging", Key=obj["Key"])
s3.delete_bucket(Bucket="wh-staging")
