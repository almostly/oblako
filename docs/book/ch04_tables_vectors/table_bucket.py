import boto3

s3tables = boto3.client("s3tables")
bucket_arn = s3tables.create_table_bucket(name="analytics")["arn"]
s3tables.create_namespace(
    tableBucketARN=bucket_arn, namespace=["web"]
)

print(bucket_arn)
namespaces = s3tables.list_namespaces(tableBucketARN=bucket_arn)
print(namespaces["namespaces"][0]["namespace"])
