"""Unit tests for KinesisService (pure config; no container required)."""

from oblako.services.kinesis import KINESIS_IMAGE, KinesisService


def test_defaults():
    svc = KinesisService()
    assert svc.host_port == 4567
    assert svc.endpoint_url == "http://localhost:4567"
    assert svc.image == KINESIS_IMAGE
    assert KINESIS_IMAGE.startswith("saidsef/aws-kinesis-local@sha256:")


def test_configured_through_entrypoint_env():
    # The entrypoint builds kinesalite's arguments from env vars; passing them as a
    # command too duplicates --path and crashes kinesalite.
    svc = KinesisService(shard_limit=200)
    assert svc.command is None
    assert svc.environment == {"PORT": "4567", "KPATH": "/data", "SHARDLIMIT": "200"}


def test_volume_uses_new_prefix():
    svc = KinesisService()
    assert "oblako-kinesis-data" in svc.volumes
