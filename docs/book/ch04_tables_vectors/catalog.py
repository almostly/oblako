import boto3
from pyiceberg.catalog import load_catalog


def open_catalog():
    s3tables = boto3.client("s3tables")
    buckets = s3tables.list_table_buckets(prefix="analytics")
    return load_catalog(
        "s3tables",
        **{
            "type": "rest",
            # the profile's S3 Tables endpoint, oblako or AWS
            "uri": s3tables.meta.endpoint_url + "/iceberg",
            "warehouse": buckets["tableBuckets"][0]["arn"],
            "rest.sigv4-enabled": "true",
            "rest.signing-name": "s3tables",
            "rest.signing-region": s3tables.meta.region_name,
        },
    )
