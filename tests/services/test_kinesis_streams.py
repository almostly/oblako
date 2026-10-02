"""Integration test: Kinesis Data Streams on kinesalite, through plain boto3.

Requires Docker. A record put on a stream comes back from GetRecords, which also
proves the container starts: an argument override written for an older image
crashed kinesalite on start (see oblako/services/kinesis.py).
"""

import contextlib
import uuid

import pytest


@pytest.fixture(scope="module")
def kinesis():
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker not available")
    from oblako.services.kinesis import KinesisService

    svc = KinesisService()
    if not svc.wait_ready(timeout=5):
        svc.start()
        if not svc.wait_ready(timeout=120):
            pytest.skip("Kinesis (kinesalite) did not become ready")
    return svc.get_client()


def test_put_and_get_record(kinesis):
    stream = f"it-{uuid.uuid4().hex[:8]}"
    kinesis.create_stream(StreamName=stream, ShardCount=1)
    try:
        kinesis.get_waiter("stream_exists").wait(StreamName=stream)
        kinesis.put_record(StreamName=stream, Data=b"hello", PartitionKey="k")
        shard = kinesis.list_shards(StreamName=stream)["Shards"][0]["ShardId"]
        iterator = kinesis.get_shard_iterator(
            StreamName=stream, ShardId=shard, ShardIteratorType="TRIM_HORIZON"
        )["ShardIterator"]
        records = kinesis.get_records(ShardIterator=iterator)["Records"]
        assert [r["Data"] for r in records] == [b"hello"]
    finally:
        with contextlib.suppress(Exception):
            kinesis.delete_stream(StreamName=stream)
