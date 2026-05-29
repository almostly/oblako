"""Local AWS Kinesis Data Streams: kinesalite via saidsef/aws-kinesis-local.

Point a boto3 ``kinesis`` client at this service and the usual API works
(create_stream, put_record, get_shard_iterator, get_records, list_streams, …).
State persists in a named volume so streams survive restarts.
"""

from __future__ import annotations

from oblako import ports
from .base import Service, PortMapping
from .boto import BotoService


@BotoService("kinesis")
class KinesisService(Service):
    """Local Kinesis Data Streams (kinesalite-backed)."""

    def __init__(self, host_port: int = ports.KINESIS, shard_limit: int = 100):
        """Initialize on host_port (4567 default) with a configurable shard limit."""
        super().__init__(
            name="kinesis",
            image="saidsef/aws-kinesis-local:latest",
            ports=[PortMapping(container_port=4567, host_port=host_port)],
            # The image's CMD is shell-mangled; override with a clean args list.
            command=[
                "--port",
                "4567",
                "--path",
                "/data",
                "--shardLimit",
                str(shard_limit),
            ],
            volumes={"oblako-kinesis-data": {"bind": "/data", "mode": "rw"}},
        )
        self.host_port = host_port

    @property
    def endpoint_url(self) -> str:
        """Return the local Kinesis endpoint URL for boto3 clients."""
        return f"http://localhost:{self.host_port}"

    def _health_check(self) -> bool:
        try:
            self.get_client().list_streams(Limit=1)
            return True
        except Exception:  # noqa: BLE001
            return False
