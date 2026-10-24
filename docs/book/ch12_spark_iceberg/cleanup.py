import boto3
from lake import bucket_arn

s3tables = boto3.client("s3tables")
arn = bucket_arn()
for table in s3tables.list_tables(tableBucketARN=arn)["tables"]:
    s3tables.delete_table(
        tableBucketARN=arn,
        namespace=table["namespace"][0],
        name=table["name"],
    )
spaces = s3tables.list_namespaces(tableBucketARN=arn)[
    "namespaces"
]
for space in spaces:
    s3tables.delete_namespace(
        tableBucketARN=arn, namespace=space["namespace"][0]
    )
s3tables.delete_table_bucket(tableBucketARN=arn)
