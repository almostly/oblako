"""Local AWS Kinesis Data Streams: kinesalite via saidsef/aws-kinesis-local.

Point a boto3 ``kinesis`` client at this service and the usual API works
(create_stream, put_record, get_shard_iterator, get_records, list_streams, …).
State persists in a named volume so streams survive restarts.
"""

from __future__ import annotations

from oblako import ports
from .base import Service, PortMapping
from .boto import BotoService, client

# Pinned by digest: the image's entrypoint changed on 2026-09-28 (it now passes
# --port/--path/--shardLimit itself, read from env vars), and an argument override
# written for the old one crashed kinesalite. A pin keeps the next rebuild from
# changing behaviour under us.
KINESIS_IMAGE = (
    "saidsef/aws-kinesis-local"
    "@sha256:a1f2be9d4356a024113bf199a0d1a352fa0d10420f35ab2f52a5d08f5920b8c9"
)


class KinesisService(Service, BotoService):
    """Local Kinesis Data Streams (kinesalite-backed)."""

    aws_services = ("kinesis",)

    def __init__(self, host_port: int = ports.KINESIS, shard_limit: int = 100):
        """Initialize on host_port (4567 default) with a configurable shard limit."""
        super().__init__(
            name="kinesis",
            image=KINESIS_IMAGE,
            ports=[PortMapping(container_port=4567, host_port=host_port)],
            # The entrypoint builds kinesalite's arguments from these variables.
            environment={
                "PORT": "4567",
                "KPATH": "/data",
                "SHARDLIMIT": str(shard_limit),
            },
            volumes={"oblako-kinesis-data": {"bind": "/data", "mode": "rw"}},
        )
        self.host_port = host_port

    @property
    def endpoint_url(self) -> str:
        """Return the local Kinesis endpoint URL for boto3 clients."""
        return f"http://localhost:{self.host_port}"

    def _health_check(self) -> bool:
        try:
            client("kinesis", self.endpoint_url).list_streams(Limit=1)
            return True
        except Exception:
            return False
