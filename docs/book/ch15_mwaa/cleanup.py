import boto3
from warehouse import run

boto3.client("mwaa").delete_environment(Name="shop")
run("DROP TABLE IF EXISTS orders")
run("DROP TABLE IF EXISTS daily_revenue")
s3 = boto3.client("s3")
for bucket in ("shop-raw", "shop-dags"):
    for obj in s3.list_objects_v2(Bucket=bucket).get(
        "Contents", []
    ):
        s3.delete_object(Bucket=bucket, Key=obj["Key"])
    s3.delete_bucket(Bucket=bucket)
