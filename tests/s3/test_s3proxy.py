"""Integration tests for S3Proxy (requires: docker compose up s3proxy)."""

import os

import boto3
import pytest
from botocore.config import Config

# override with OBLAKO_TEST_S3_ENDPOINT to run against an isolated S3Proxy
S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")


@pytest.fixture
def s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


@pytest.fixture
def bucket(s3):
    name = "test-oblako"
    try:
        s3.create_bucket(Bucket=name)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    yield name
    # cleanup
    resp = s3.list_objects_v2(Bucket=name)
    for obj in resp.get("Contents", []):
        s3.delete_object(Bucket=name, Key=obj["Key"])
    s3.delete_bucket(Bucket=name)


def test_put_and_get_object(s3, bucket):
    s3.put_object(Bucket=bucket, Key="hello.txt", Body=b"world")
    resp = s3.get_object(Bucket=bucket, Key="hello.txt")
    assert resp["Body"].read() == b"world"


def test_list_objects(s3, bucket):
    s3.put_object(Bucket=bucket, Key="a.csv", Body=b"1,2,3")
    s3.put_object(Bucket=bucket, Key="b.csv", Body=b"4,5,6")
    resp = s3.list_objects_v2(Bucket=bucket)
    keys = [obj["Key"] for obj in resp["Contents"]]
    assert sorted(keys) == ["a.csv", "b.csv"]


def test_delete_object(s3, bucket):
    s3.put_object(Bucket=bucket, Key="temp.txt", Body=b"delete me")
    s3.delete_object(Bucket=bucket, Key="temp.txt")
    resp = s3.list_objects_v2(Bucket=bucket)
    assert resp.get("KeyCount", 0) == 0


def test_paginates_more_than_1000_partitioned_keys(s3):
    # Hive-style keys contain "=". S3Proxy 4.0 returned the continuation token
    # URL-encoded and didn't decode it, so every page restarted from the first
    # key: a manual loop never ended and boto3's paginator raised.
    import concurrent.futures

    name = "test-oblako-pagination"
    try:
        s3.create_bucket(Bucket=name)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    keys = [
        f"events/dt=2026-01-{day:02d}/part-{i:04d}.parquet"
        for day in (1, 2, 3)
        for i in range(400)
    ]
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        list(pool.map(lambda k: s3.put_object(Bucket=name, Key=k, Body=b"x"), keys))
    try:
        first = s3.list_objects_v2(Bucket=name, Prefix="events/")
        assert first["IsTruncated"]
        assert "%" not in first["NextContinuationToken"]  # real S3 doesn't encode it
        listed = [
            obj["Key"]
            for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=name, Prefix="events/"
            )
            for obj in page.get("Contents", [])
        ]
        assert sorted(listed) == sorted(keys)
    finally:
        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            list(pool.map(lambda k: s3.delete_object(Bucket=name, Key=k), keys))
        s3.delete_bucket(Bucket=name)
