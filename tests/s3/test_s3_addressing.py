"""Integration test: virtual-hosted addressing, UTF-8 keys and S3 Control tags.

Requires S3 (oblako up s3). The AWS SDKs outside Python address buckets as
``bucket.localhost:9000`` and read bucket tags through S3 Control; Terraform and
Pulumi use both. Keys are any UTF-8 text, as on S3.
"""

import uuid

import boto3
import pytest
from botocore.config import Config

ENDPOINT = "http://localhost:9000"
CREDS = dict(
    region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
)


def _s3(style):
    config = Config(
        s3={"addressing_style": style},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    return boto3.client("s3", endpoint_url=ENDPOINT, config=config, **CREDS)


@pytest.fixture
def bucket():
    name = f"addressing-{uuid.uuid4().hex[:8]}"
    path = _s3("path")
    path.create_bucket(Bucket=name)
    yield name
    for obj in path.list_objects_v2(Bucket=name).get("Contents", []):
        path.delete_object(Bucket=name, Key=obj["Key"])
    path.delete_bucket(Bucket=name)


KEYS = ["plain.txt", "dir/with space.txt", "plus+and%2Fslash.txt", "café/naïve.txt"]


def test_virtual_hosted_and_path_style_see_the_same_objects(bucket):
    vhost, path = _s3("virtual"), _s3("path")
    for key in KEYS:
        vhost.put_object(Bucket=bucket, Key=key, Body=key.encode())
    listed = sorted(o["Key"] for o in path.list_objects_v2(Bucket=bucket)["Contents"])
    assert listed == sorted(KEYS)
    for key in KEYS:
        assert vhost.get_object(Bucket=bucket, Key=key)["Body"].read() == key.encode()


def test_virtual_hosted_create_bucket_and_tagging():
    vhost, path = _s3("virtual"), _s3("path")
    name = f"vhost-{uuid.uuid4().hex[:8]}"
    vhost.create_bucket(Bucket=name)
    try:
        assert name in [b["Name"] for b in path.list_buckets()["Buckets"]]
        vhost.put_object(Bucket=name, Key="k", Body=b"x")
        vhost.put_object_tagging(
            Bucket=name, Key="k", Tagging={"TagSet": [{"Key": "a", "Value": "1"}]}
        )
        assert path.get_object_tagging(Bucket=name, Key="k")["TagSet"] == [
            {"Key": "a", "Value": "1"}
        ]
        vhost.delete_object(Bucket=name, Key="k")
    finally:
        path.delete_bucket(Bucket=name)


def test_s3_control_tags_are_the_bucket_tags(bucket):
    control = boto3.client("s3control", endpoint_url=ENDPOINT, **CREDS)
    arn = f"arn:aws:s3:::{bucket}"
    control.tag_resource(
        AccountId="123456789012",
        ResourceArn=arn,
        Tags=[{"Key": "team", "Value": "data"}, {"Key": "env", "Value": "dev"}],
    )
    got = control.list_tags_for_resource(AccountId="123456789012", ResourceArn=arn)
    assert sorted(t["Key"] for t in got["Tags"]) == ["env", "team"]
    # the same tags through the S3 API
    tagset = _s3("path").get_bucket_tagging(Bucket=bucket)["TagSet"]
    assert {t["Key"]: t["Value"] for t in tagset} == {"team": "data", "env": "dev"}
    control.untag_resource(AccountId="123456789012", ResourceArn=arn, TagKeys=["env"])
    got = control.list_tags_for_resource(AccountId="123456789012", ResourceArn=arn)
    assert got["Tags"] == [{"Key": "team", "Value": "data"}]
