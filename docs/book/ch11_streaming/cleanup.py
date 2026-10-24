import boto3

from warehouse import query

firehose = boto3.client("firehose")
for name in ("clicks-to-s3", "views-to-redshift"):
    try:
        firehose.delete_delivery_stream(DeliveryStreamName=name)
    except firehose.exceptions.ResourceNotFoundException:
        pass

kinesis = boto3.client("kinesis")
try:
    kinesis.delete_stream(StreamName="clicks")
except kinesis.exceptions.ResourceNotFoundException:
    pass

glue = boto3.client("glue")
try:
    glue.delete_table(DatabaseName="web", Name="stream_clicks")
except glue.exceptions.EntityNotFoundException:
    pass

s3 = boto3.client("s3")
for prefix in ("stream-clicks/", "stream-errors/", "staging/"):
    listing = s3.list_objects_v2(Bucket="events", Prefix=prefix)
    for obj in listing.get("Contents", []):
        s3.delete_object(Bucket="events", Key=obj["Key"])
query("DROP TABLE IF EXISTS page_views")
