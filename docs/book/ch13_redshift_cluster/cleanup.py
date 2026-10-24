import boto3

redshift = boto3.client("redshift")
try:
    redshift.delete_cluster(
        ClusterIdentifier="analytics",
        SkipFinalClusterSnapshot=True,
    )
except redshift.exceptions.ClusterNotFoundFault:
    pass

s3 = boto3.client("s3")
try:
    listing = s3.list_objects_v2(Bucket="warehouse-staging")
except s3.exceptions.NoSuchBucket:
    listing = None
if listing is not None:
    for obj in listing.get("Contents", []):
        s3.delete_object(
            Bucket="warehouse-staging", Key=obj["Key"]
        )
    s3.delete_bucket(Bucket="warehouse-staging")
