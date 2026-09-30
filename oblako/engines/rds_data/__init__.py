"""Local AWS RDS Data API over the RDS Postgres engine.

Exposes the `rds-data` wire protocol so a real boto3 client works:

    import boto3
    rd = boto3.client("rds-data", endpoint_url="http://localhost:8006",
                      region_name="us-east-1",
                      aws_access_key_id="test", aws_secret_access_key="test")
    rd.execute_statement(resourceArn="arn:...", secretArn="arn:...",
                         database="oblako", sql="SELECT 1 AS n",
                         includeResultMetadata=True)
"""

from __future__ import annotations

import threading
import time

from oblako.engines.identity import claim_port, identify, is_engine

from .app import app, create_app
from .executor import RdsDataExecutor

__all__ = ["app", "create_app", "RdsDataExecutor", "start_in_thread", "is_running"]

DEFAULT_PORT = 8006

_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if an rds-data server is responding on the given port."""
    return is_engine(port, "rds_data", timeout)


def start_in_thread(
    port: int = DEFAULT_PORT, executor: RdsDataExecutor | None = None
) -> str:
    """Start the rds-data server in a daemon thread (idempotent). Returns the URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "rds_data")
        application = create_app(executor)
        config = uvicorn.Config(
            identify(application, "rds_data"),
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
    raise RuntimeError(f"rds-data server did not start on port {port}")
