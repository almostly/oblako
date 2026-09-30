"""Local Kinesis Data Firehose (DirectPut -> S3) as an in-process engine.

from oblako.engines.firehose import get_client
fh = get_client()
fh.create_delivery_stream(
    DeliveryStreamName="events",
    ExtendedS3DestinationConfiguration={
        "RoleARN": "arn:aws:iam::000000000000:role/x",
        "BucketARN": "arn:aws:s3:::my-bucket",
        "Prefix": "events/",
        "BufferingHints": {"IntervalInSeconds": 2, "SizeInMBs": 5},
    },
)
fh.put_record(DeliveryStreamName="events", Record={"Data": b'{"a":1}'})
"""

from __future__ import annotations

import threading
import time

from oblako import ports
from oblako.engines.identity import claim_port, identify, is_engine

from .app import FirehoseExecutor, app, create_app

__all__ = [
    "app",
    "create_app",
    "FirehoseExecutor",
    "start_in_thread",
    "is_running",
    "get_client",
]

DEFAULT_PORT = ports.FIREHOSE

_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if a firehose server is reachable on the port."""
    return is_engine(port, "firehose", timeout)


def start_in_thread(port: int = DEFAULT_PORT) -> str:
    """Start the firehose server in a daemon thread (idempotent). Returns its URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "firehose")
        config = uvicorn.Config(
            identify(create_app(), "firehose"),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        _servers[port] = server

    deadline = time.time() + 10
    while time.time() < deadline:
        if is_running(port):
            return url
        time.sleep(0.1)
    raise RuntimeError(f"firehose server did not start on port {port}")


def get_client(port: int = DEFAULT_PORT):
    """Return a boto3 ``firehose`` client wired to the local server."""
    import os

    import boto3

    start_in_thread(port)
    return boto3.client(
        "firehose",
        endpoint_url=f"http://localhost:{port}",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    )
