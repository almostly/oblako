"""Local Amazon Athena over the Trino engine.

from oblako.engines.athena import get_client
athena = get_client()
qid = athena.start_query_execution(
    QueryString="SELECT 1 AS n",
    ResultConfiguration={"OutputLocation": "s3://oblako-athena/results/"},
)["QueryExecutionId"]
# poll get_query_execution until SUCCEEDED, then get_query_results
"""

from __future__ import annotations

import threading
import time

from oblako import ports
from oblako.engines.identity import claim_port, identify, is_engine

from .app import AthenaExecutor, app, create_app

__all__ = [
    "app",
    "create_app",
    "AthenaExecutor",
    "start_in_thread",
    "is_running",
    "get_client",
]

DEFAULT_PORT = ports.ATHENA

_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if an athena server is reachable on the port."""
    return is_engine(port, "athena", timeout)


def start_in_thread(port: int = DEFAULT_PORT) -> str:
    """Start the athena server in a daemon thread (idempotent). Returns its URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "athena")
        config = uvicorn.Config(
            identify(create_app(), "athena"),
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
    raise RuntimeError(f"athena server did not start on port {port}")


def get_client(port: int = DEFAULT_PORT):
    """Return a boto3 ``athena`` client wired to the local server."""
    import os

    import boto3

    start_in_thread(port)
    return boto3.client(
        "athena",
        endpoint_url=f"http://localhost:{port}",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    )
