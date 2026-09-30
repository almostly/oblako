"""Local AWS Redshift Data API over the oblako/redshift container.

Exposes the `redshift-data` wire protocol so a real boto3 client works:

    import boto3
    rd = boto3.client("redshift-data", endpoint_url="http://localhost:8002",
                      region_name="us-east-1",
                      aws_access_key_id="test", aws_secret_access_key="test")
    q = rd.execute_statement(Database="oblako", Sql="SELECT 1 AS n")
    rd.get_statement_result(Id=q["Id"])["Records"]
"""

from __future__ import annotations

import threading
import time

from oblako.engines.identity import claim_port, identify, is_engine

from .app import app, create_app
from .executor import RedshiftDataExecutor

__all__ = ["app", "create_app", "RedshiftDataExecutor", "start_in_thread", "is_running"]

DEFAULT_PORT = 8002

_servers: dict[int, "object"] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if a redshift-data server is reachable on the port."""
    return is_engine(port, "redshift_data", timeout)


def start_in_thread(
    port: int = DEFAULT_PORT, executor: RedshiftDataExecutor | None = None
) -> str:
    """Start the redshift-data server in a daemon thread (idempotent).

    ``port`` is the HTTP server port; ``executor`` configures the the Redshift engine
    backend (defaults to env-configured localhost:5439). Returns the endpoint
    URL. Safe to call repeatedly; only starts once per port.
    """
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "redshift_data")
        application = create_app(executor)
        config = uvicorn.Config(
            identify(application, "redshift_data"),
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
    raise RuntimeError(f"redshift-data server did not start on port {port}")
