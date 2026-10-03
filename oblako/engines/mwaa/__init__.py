"""Amazon MWAA on oblako: AWS's own Airflow containers behind the MWAA API.

``CreateEnvironment`` starts an environment's containers from the images AWS
publishes as source (see ``environments``); DAGs sync from the S3 source bucket,
and ``InvokeRestApi`` reaches Airflow's REST API. ``oblako up mwaa`` starts it.

    from oblako.engines.mwaa import get_client
    mwaa = get_client()  # boto3 mwaa, pointed at the engine
"""

from __future__ import annotations

import threading
import time

from oblako import ports
from oblako.engines.identity import claim_port, identify, is_engine

from .app import app, create_app

__all__ = ["app", "create_app", "get_client", "is_running", "start_in_thread"]

DEFAULT_PORT = ports.MWAA

_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if the engine is accepting requests on the port."""
    return is_engine(port, "mwaa", timeout)


def start_in_thread(port: int = DEFAULT_PORT) -> str:
    """Start the MWAA engine in a daemon thread (idempotent). Returns its URL.

    It listens on IPv6 and IPv4: boto3 calls ``api.localhost`` and
    ``env.localhost``, which macOS resolves to ``::1``.
    """
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "mwaa")
        config = uvicorn.Config(
            identify(create_app(), "mwaa"),
            host="::",
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
    raise RuntimeError(f"mwaa engine did not start on port {port}")


def get_client(port: int = DEFAULT_PORT):
    """Return a boto3 ``mwaa`` client wired to the local engine."""
    import os

    import boto3

    start_in_thread(port)
    return boto3.client(
        "mwaa",
        endpoint_url=f"http://localhost:{port}",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    )
