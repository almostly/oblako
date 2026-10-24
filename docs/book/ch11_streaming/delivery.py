import boto3

ROLE = "arn:aws:iam::123456789012:role/firehose-delivery"
ERRORS = "stream-errors/!{firehose:error-output-type}/"
kinesis = boto3.client("kinesis")
firehose = boto3.client("firehose")

source = kinesis.describe_stream_summary(StreamName="clicks")
firehose.create_delivery_stream(
    DeliveryStreamName="clicks-to-s3",
    DeliveryStreamType="KinesisStreamAsSource",
    KinesisStreamSourceConfiguration={
        "KinesisStreamARN": source["StreamDescriptionSummary"][
            "StreamARN"
        ],
        "RoleARN": ROLE,
    },
    ExtendedS3DestinationConfiguration={
        "RoleARN": ROLE,
        "BucketARN": "arn:aws:s3:::events",
        "Prefix": "stream-clicks/dt=!{timestamp:yyyy-MM-dd}/",
        "ErrorOutputPrefix": ERRORS,
        "BufferingHints": {
            "IntervalInSeconds": 60,
            "SizeInMBs": 64,
        },
        "DataFormatConversionConfiguration": {
            "SchemaConfiguration": {
                "DatabaseName": "web",
                "TableName": "stream_clicks",
                "RoleARN": ROLE,
            },
            "InputFormatConfiguration": {
                "Deserializer": {"OpenXJsonSerDe": {}}
            },
            "OutputFormatConfiguration": {
                "Serializer": {"ParquetSerDe": {}}
            },
        },
    },
)
description = firehose.describe_delivery_stream(
    DeliveryStreamName="clicks-to-s3"
)["DeliveryStreamDescription"]
print(
    description["DeliveryStreamStatus"],
    description["DeliveryStreamType"],
)
