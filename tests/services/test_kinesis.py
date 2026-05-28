"""Unit tests for KinesisService (pure config; no container required)."""

from oblako.services.kinesis import KinesisService


def test_defaults():
    svc = KinesisService()
    assert svc.host_port == 4567
    assert svc.endpoint_url == "http://localhost:4567"
    assert svc.image == "saidsef/aws-kinesis-local:latest"


def test_command_passes_clean_args():
    # The image's own CMD is shell-mangled; we override with a clean list.
    svc = KinesisService(shard_limit=200)
    assert svc.command == ["--port", "4567", "--path", "/data", "--shardLimit", "200"]


def test_volume_uses_new_prefix():
    svc = KinesisService()
    assert "oblako-kinesis-data" in svc.volumes
