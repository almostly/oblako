import boto3

s3tables = boto3.client("s3tables")
bucket = s3tables.create_table_bucket(name="lakehouse")
print(bucket["arn"])
