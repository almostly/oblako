"""Integration test: Kinesis Data Firehose delivers records to S3.

Requires S3Proxy (no Docker for the engine itself). A DirectPut delivery stream
with an S3 destination buffers PutRecord/PutRecordBatch data and stages it as S3
objects on the buffering interval — unmodified boto3 ``firehose``. Override
OBLAKO_TEST_S3_ENDPOINT to point at an isolated S3Proxy.
"""

import json
import os
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "firehose-ci"
ROLE = "arn:aws:iam::000000000000:role/firehose"


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        ),
    )


def test_firehose_delivers_records_to_s3():
    try:
        _s3().list_buckets()
    except Exception:
        pytest.skip("S3Proxy not available")

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.engines.firehose import get_client

    s3 = _s3()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass

    fh = get_client()
    stream = "events"
    fh.create_delivery_stream(
        DeliveryStreamName=stream,
        DeliveryStreamType="DirectPut",
        ExtendedS3DestinationConfiguration={
            "RoleARN": ROLE,
            "BucketARN": f"arn:aws:s3:::{BUCKET}",
            "Prefix": "events/",
            "BufferingHints": {"IntervalInSeconds": 1, "SizeInMBs": 5},
            "CompressionFormat": "UNCOMPRESSED",
        },
    )
    try:
        desc = fh.describe_delivery_stream(DeliveryStreamName=stream)[
            "DeliveryStreamDescription"
        ]
        assert desc["DeliveryStreamStatus"] == "ACTIVE"
        assert stream in fh.list_delivery_streams()["DeliveryStreamNames"]

        fh.put_record(
            DeliveryStreamName=stream, Record={"Data": b'{"id":1,"amt":10}\n'}
        )
        fh.put_record_batch(
            DeliveryStreamName=stream,
            Records=[
                {"Data": b'{"id":2,"amt":20}\n'},
                {"Data": b'{"id":3,"amt":30}\n'},
            ],
        )

        # the delivery loop stages the buffered records to S3 within the interval
        objects = []
        for _ in range(20):
            objects = s3.list_objects_v2(Bucket=BUCKET, Prefix="events/").get(
                "Contents", []
            )
            if objects:
                break
            time.sleep(0.5)
        assert objects, "firehose did not stage any object to S3"

        records = []
        for obj in objects:
            body = s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read().decode()
            records += [json.loads(line) for line in body.splitlines() if line.strip()]
        ids = sorted(r["id"] for r in records)
        assert ids == [1, 2, 3]
        assert {r["amt"] for r in records} == {10, 20, 30}

        # the key follows the Firehose prefix/YYYY/MM/DD/HH/name-ts-uuid layout
        assert objects[0]["Key"].startswith("events/")
        assert f"/{stream}-" in objects[0]["Key"]
    finally:
        fh.delete_delivery_stream(DeliveryStreamName=stream)
