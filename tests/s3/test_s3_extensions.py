"""Integration tests: S3 tagging and Inventory through the :9000 front.

Requires ``oblako up s3`` (nginx on :9000, S3Proxy on :9001, the extensions
engine). Uses a plain boto3 client, the way a reader would.
"""

import gzip
import json
import os
import time
import uuid

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")


@pytest.fixture(scope="module")
def s3():
    client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    try:
        client.list_buckets()
        client.get_bucket_tagging(Bucket=f"probe-{uuid.uuid4().hex[:8]}")
    except client.exceptions.ClientError as err:
        if err.response["Error"]["Code"] == "NotImplemented":
            pytest.skip("S3 is running without the tagging / Inventory front")
    except Exception:
        pytest.skip("S3 not reachable")
    return client


@pytest.fixture
def bucket(s3):
    name = f"tags-{uuid.uuid4().hex[:8]}"
    s3.create_bucket(Bucket=name)
    yield name
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=name):
        for obj in page.get("Contents", []):
            s3.delete_object(Bucket=name, Key=obj["Key"])
    s3.delete_bucket(Bucket=name)


def _tags(s3, bucket, key):
    return {
        t["Key"]: t["Value"]
        for t in s3.get_object_tagging(Bucket=bucket, Key=key)["TagSet"]
    }


def test_put_object_with_tags_and_metadata(s3, bucket):
    s3.put_object(
        Bucket=bucket,
        Key="raw/orders.csv",
        Body=b"id\n1\n",
        Metadata={"source-job": "ingest"},
        Tagging="stage=raw&owner=etl",
    )
    assert _tags(s3, bucket, "raw/orders.csv") == {"stage": "raw", "owner": "etl"}
    # metadata keys come back lowercase, as from S3 (the front must not
    # rewrite header case)
    head = s3.head_object(Bucket=bucket, Key="raw/orders.csv")
    assert head["Metadata"] == {"source-job": "ingest"}
    assert s3.get_object(Bucket=bucket, Key="raw/orders.csv")["Body"].read() == (
        b"id\n1\n"
    )


def test_tagging_lifecycle(s3, bucket):
    s3.put_object(Bucket=bucket, Key="a.csv", Body=b"x")
    assert _tags(s3, bucket, "a.csv") == {}
    s3.put_object_tagging(
        Bucket=bucket,
        Key="a.csv",
        Tagging={"TagSet": [{"Key": "stage", "Value": "curated"}]},
    )
    assert _tags(s3, bucket, "a.csv") == {"stage": "curated"}
    s3.delete_object_tagging(Bucket=bucket, Key="a.csv")
    assert _tags(s3, bucket, "a.csv") == {}


def test_overwrite_drops_tags_and_copy_carries_them(s3, bucket):
    s3.put_object(Bucket=bucket, Key="src", Body=b"x", Tagging="stage=raw")
    s3.copy_object(
        Bucket=bucket, Key="copy", CopySource={"Bucket": bucket, "Key": "src"}
    )
    assert _tags(s3, bucket, "copy") == {"stage": "raw"}
    s3.copy_object(
        Bucket=bucket,
        Key="replaced",
        CopySource={"Bucket": bucket, "Key": "src"},
        TaggingDirective="REPLACE",
        Tagging="stage=archived",
    )
    assert _tags(s3, bucket, "replaced") == {"stage": "archived"}
    time.sleep(1.1)  # Last-Modified has one-second resolution
    s3.put_object(Bucket=bucket, Key="src", Body=b"new")
    assert _tags(s3, bucket, "src") == {}  # a new object version has no tags


def test_invalid_and_missing(s3, bucket):
    with pytest.raises(s3.exceptions.ClientError, match="InvalidTag"):
        s3.put_object(
            Bucket=bucket,
            Key="x",
            Body=b"x",
            Tagging="&".join(f"k{i}=v" for i in range(11)),
        )
    with pytest.raises(s3.exceptions.ClientError, match="NoSuchKey"):
        s3.get_object_tagging(Bucket=bucket, Key="missing")


def test_multipart_upload_keeps_tags(s3, bucket):
    upload = s3.create_multipart_upload(Bucket=bucket, Key="big", Tagging="stage=raw")
    part = s3.upload_part(
        Bucket=bucket,
        Key="big",
        UploadId=upload["UploadId"],
        PartNumber=1,
        Body=b"x" * 1024,
    )
    s3.complete_multipart_upload(
        Bucket=bucket,
        Key="big",
        UploadId=upload["UploadId"],
        MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": part["ETag"]}]},
    )
    assert _tags(s3, bucket, "big") == {"stage": "raw"}


def test_bucket_tagging(s3, bucket):
    with pytest.raises(s3.exceptions.ClientError, match="NoSuchTagSet"):
        s3.get_bucket_tagging(Bucket=bucket)
    s3.put_bucket_tagging(
        Bucket=bucket, Tagging={"TagSet": [{"Key": "team", "Value": "ds"}]}
    )
    assert s3.get_bucket_tagging(Bucket=bucket)["TagSet"] == [
        {"Key": "team", "Value": "ds"}
    ]
    s3.delete_bucket_tagging(Bucket=bucket)
    with pytest.raises(s3.exceptions.ClientError, match="NoSuchTagSet"):
        s3.get_bucket_tagging(Bucket=bucket)


def test_listing_with_tagging_in_a_prefix_still_lists(s3, bucket):
    s3.put_object(Bucket=bucket, Key="tagging/readme.txt", Body=b"x")
    listed = s3.list_objects_v2(Bucket=bucket, Prefix="tagging")
    assert [o["Key"] for o in listed["Contents"]] == ["tagging/readme.txt"]


def test_inventory_configuration_and_report(s3, bucket):
    dest = f"{bucket}-inv"
    s3.create_bucket(Bucket=dest)
    try:
        s3.put_object(Bucket=bucket, Key="raw/a.csv", Body=b"abc")
        s3.put_object(Bucket=bucket, Key="raw/b.csv", Body=b"defg")
        s3.put_bucket_inventory_configuration(
            Bucket=bucket,
            Id="daily",
            InventoryConfiguration={
                "Destination": {
                    "S3BucketDestination": {
                        "Bucket": f"arn:aws:s3:::{dest}",
                        "Format": "CSV",
                        "Prefix": "inv",
                    }
                },
                "IsEnabled": True,
                "Id": "daily",
                "IncludedObjectVersions": "Current",
                "Schedule": {"Frequency": "Daily"},
                "OptionalFields": ["Size", "StorageClass"],
            },
        )
        got = s3.get_bucket_inventory_configuration(Bucket=bucket, Id="daily")
        assert (
            got["InventoryConfiguration"]["Destination"]["S3BucketDestination"][
                "Format"
            ]
            == "CSV"
        )
        listed = s3.list_bucket_inventory_configurations(Bucket=bucket)
        assert [c["Id"] for c in listed["InventoryConfigurationList"]] == ["daily"]

        # the first report is written right away (S3 takes up to 48 h)
        for _ in range(50):
            keys = [
                o["Key"] for o in s3.list_objects_v2(Bucket=dest).get("Contents", [])
            ]
            if any(k.endswith("manifest.json") for k in keys):
                break
            time.sleep(0.2)
        manifest_key = next(k for k in keys if k.endswith("manifest.json"))
        assert manifest_key.startswith(f"inv/{bucket}/daily/")
        manifest = json.loads(
            s3.get_object(Bucket=dest, Key=manifest_key)["Body"].read()
        )
        assert manifest["fileFormat"] == "CSV"
        assert manifest["fileSchema"] == "Bucket, Key, Size, StorageClass"
        data = s3.get_object(Bucket=dest, Key=manifest["files"][0]["key"])[
            "Body"
        ].read()
        rows = gzip.decompress(data).decode().strip().splitlines()
        assert rows == [
            f'"{bucket}","raw/a.csv","3","STANDARD"',
            f'"{bucket}","raw/b.csv","4","STANDARD"',
        ]

        s3.delete_bucket_inventory_configuration(Bucket=bucket, Id="daily")
        listed = s3.list_bucket_inventory_configurations(Bucket=bucket)
        assert listed.get("InventoryConfigurationList", []) == []
    finally:
        for obj in s3.list_objects_v2(Bucket=dest).get("Contents", []):
            s3.delete_object(Bucket=dest, Key=obj["Key"])
        s3.delete_bucket(Bucket=dest)


# -----------------------------------------------------------------------------
# Bucket policy, event notifications, temporary credentials
# -----------------------------------------------------------------------------
MOTO = os.environ.get("OBLAKO_TEST_MOTO_ENDPOINT", "http://localhost:5500")
CREDS = dict(
    region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
)


def test_bucket_policy_is_stored_and_returned(s3, bucket):
    with pytest.raises(s3.exceptions.ClientError, match="NoSuchBucketPolicy"):
        s3.get_bucket_policy(Bucket=bucket)
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicRead",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/curated/*",
            }
        ],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    assert json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"]) == policy
    assert s3.get_bucket_policy_status(Bucket=bucket)["PolicyStatus"]["IsPublic"]
    with pytest.raises(s3.exceptions.ClientError, match="MalformedPolicy"):
        s3.put_bucket_policy(Bucket=bucket, Policy="not json")
    s3.delete_bucket_policy(Bucket=bucket)
    with pytest.raises(s3.exceptions.ClientError, match="NoSuchBucketPolicy"):
        s3.get_bucket_policy(Bucket=bucket)


def test_notifications_to_sqs_honor_filters(s3, bucket):
    sqs = boto3.client("sqs", endpoint_url=MOTO, **CREDS)
    queue = sqs.create_queue(QueueName=f"{bucket}-events")["QueueUrl"]
    try:
        arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        s3.put_bucket_notification_configuration(
            Bucket=bucket,
            NotificationConfiguration={
                "QueueConfigurations": [
                    {
                        "QueueArn": arn,
                        "Events": ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"],
                        "Filter": {
                            "Key": {
                                "FilterRules": [
                                    {"Name": "prefix", "Value": "raw/"},
                                    {"Name": "suffix", "Value": ".csv"},
                                ]
                            }
                        },
                    }
                ]
            },
        )
        got = s3.get_bucket_notification_configuration(Bucket=bucket)
        assert got["QueueConfigurations"][0]["QueueArn"] == arn
        s3.put_object(Bucket=bucket, Key="raw/orders.csv", Body=b"id\n1\n")
        s3.put_object(Bucket=bucket, Key="raw/notes.txt", Body=b"suffix: no event")
        s3.put_object(Bucket=bucket, Key="curated/x.csv", Body=b"prefix: no event")
        s3.delete_object(Bucket=bucket, Key="raw/orders.csv")
        events = []
        deadline = time.time() + 30
        while len(events) < 2 and time.time() < deadline:
            for msg in sqs.receive_message(
                QueueUrl=queue, MaxNumberOfMessages=10, WaitTimeSeconds=1
            ).get("Messages", []):
                rec = json.loads(msg["Body"])["Records"][0]
                events.append((rec["eventName"], rec["s3"]["object"]["key"]))
        assert sorted(events) == [
            ("ObjectCreated:Put", "raw/orders.csv"),
            ("ObjectRemoved:Delete", "raw/orders.csv"),
        ]
    finally:
        sqs.delete_queue(QueueUrl=queue)


def test_temporary_credentials_are_accepted(bucket):
    # boto3 sends x-amz-security-token with session credentials (Lambda, SSO,
    # assumed roles); S3Proxy rejects it, so the front drops it
    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_session_token="session-token",
        config=Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
        **CREDS,
    )
    s3.put_object(Bucket=bucket, Key="t.txt", Body=b"x", Tagging="a=b")
    assert s3.get_object(Bucket=bucket, Key="t.txt")["Body"].read() == b"x"


LAMBDA_CODE = """
import boto3, os, urllib.parse
def handler(event, context):
    s3 = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"])
    for r in event["Records"]:
        b = r["s3"]["bucket"]["name"]
        k = urllib.parse.unquote_plus(r["s3"]["object"]["key"])
        body = s3.get_object(Bucket=b, Key=k)["Body"].read()
        s3.put_object(Bucket=b, Key="curated/" + k.split("/", 1)[1], Body=body.upper())
"""


def test_object_created_invokes_a_lambda_that_writes_back(s3, bucket):
    # the event-driven pipeline: a CSV lands in raw/, the notification invokes the
    # Lambda (in moto, plain boto3 inside), and the result appears in curated/
    import io
    import zipfile

    try:
        import docker

        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker not available for moto's Lambda runtime")
    lam = boto3.client("lambda", endpoint_url=MOTO, **CREDS)
    iam = boto3.client("iam", endpoint_url=MOTO, **CREDS)
    name = f"{bucket}-fn"
    role = iam.create_role(
        RoleName=name,
        AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": []}),
    )["Role"]["Arn"]
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as zf:
        zf.writestr("handler.py", LAMBDA_CODE)
    fn = lam.create_function(
        FunctionName=name,
        Runtime="python3.12",
        Role=role,
        Handler="handler.handler",
        Code={"ZipFile": zipped.getvalue()},
        Timeout=60,
        Environment={
            "Variables": {"AWS_ENDPOINT_URL_S3": "http://host.docker.internal:9000"}
        },
    )["FunctionArn"]
    try:
        s3.put_bucket_notification_configuration(
            Bucket=bucket,
            NotificationConfiguration={
                "LambdaFunctionConfigurations": [
                    {
                        "LambdaFunctionArn": fn,
                        "Events": ["s3:ObjectCreated:*"],
                        "Filter": {
                            "Key": {
                                "FilterRules": [{"Name": "prefix", "Value": "raw/"}]
                            }
                        },
                    }
                ]
            },
        )
        s3.put_object(Bucket=bucket, Key="raw/products.csv", Body=b"id,price\n1,9.5\n")
        deadline = time.time() + 300  # first run may pull the Lambda runtime image
        while time.time() < deadline:
            found = s3.list_objects_v2(Bucket=bucket, Prefix="curated/").get("Contents")
            if found:
                break
            time.sleep(1)
        assert s3.get_object(Bucket=bucket, Key="curated/products.csv")[
            "Body"
        ].read() == (b"ID,PRICE\n1,9.5\n")
    finally:
        lam.delete_function(FunctionName=name)
        iam.delete_role(RoleName=name)


def test_create_bucket_you_own_succeeds_in_us_east_1(s3):
    """S3 in us-east-1 answers 200 for a bucket you already own; elsewhere 409."""
    name = f"owned-{uuid.uuid4().hex[:8]}"
    s3.create_bucket(Bucket=name)
    s3.create_bucket(Bucket=name)  # us-east-1: no error, as on S3
    with pytest.raises(s3.exceptions.BucketAlreadyOwnedByYou):
        s3.create_bucket(
            Bucket=name, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"}
        )
    s3.delete_bucket(Bucket=name)
