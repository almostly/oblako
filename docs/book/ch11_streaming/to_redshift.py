import boto3

from warehouse import query

ROLE = "arn:aws:iam::123456789012:role/firehose-delivery"
query("DROP TABLE IF EXISTS page_views")
query("""
    CREATE TABLE page_views (
        "user" varchar(32),
        page   varchar(64),
        at     timestamp
    )""")

firehose = boto3.client("firehose")
firehose.create_delivery_stream(
    DeliveryStreamName="views-to-redshift",
    DeliveryStreamType="DirectPut",
    RedshiftDestinationConfiguration={
        "RoleARN": ROLE,
        "ClusterJDBCURL": "jdbc:redshift://localhost:5439/oblako",
        "Username": "oblako",
        "Password": "oblako",
        "CopyCommand": {
            "DataTableName": "page_views",
            "CopyOptions": "JSON 'auto' GZIP TIMEFORMAT 'auto'",
        },
        "S3Configuration": {
            "RoleARN": ROLE,
            "BucketARN": "arn:aws:s3:::events",
            "Prefix": "staging/page-views/",
            "CompressionFormat": "GZIP",
            "BufferingHints": {
                "IntervalInSeconds": 60,
                "SizeInMBs": 5,
            },
        },
    },
)
print("views-to-redshift created")
