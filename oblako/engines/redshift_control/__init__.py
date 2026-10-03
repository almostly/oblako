"""Redshift control plane that runs real multi-node clusters.

A proxy in front of moto's Redshift API: a single-node cluster's endpoint is the
shared engine on 5439, and a multi-node cluster runs as its own Citus cluster (see
``app`` and ``clusters``). ``RedshiftService.start`` starts it, so
``oblako up redshift`` is enough.

    from oblako.engines.redshift_control import get_client
    redshift = get_client()  # boto3 redshift, pointed at the proxy
"""

from __future__ import annotations

import threading
import time

from oblako import ports
from oblako.engines.identity import claim_port, identify, is_engine

from .app import RedshiftControlProxy, app, create_app

__all__ = [
    "app",
    "create_app",
    "RedshiftControlProxy",
    "start_in_thread",
    "is_running",
    "get_client",
]

DEFAULT_PORT = ports.REDSHIFT_CONTROL

_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if the proxy is accepting requests on the port."""
    return is_engine(port, "redshift_control", timeout)


def start_in_thread(port: int = DEFAULT_PORT, backend_url: str | None = None) -> str:
    """Start the redshift-control proxy in a daemon thread (idempotent). Returns its URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "redshift_control")
        config = uvicorn.Config(
            identify(create_app(backend_url), "redshift_control"),
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
    raise RuntimeError(f"redshift-control proxy did not start on port {port}")


def get_client(port: int = DEFAULT_PORT, backend_url: str | None = None):
    """Return a boto3 ``redshift`` client wired to the local proxy."""
    import os

    import boto3

    start_in_thread(port, backend_url)
    return boto3.client(
        "redshift",
        endpoint_url=f"http://localhost:{port}",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    )
