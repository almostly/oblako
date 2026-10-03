"""ECS API that runs tasks as real containers, in front of moto (see ``app``).

``oblako up ecs`` starts it on its port; the ``oblako`` profile points
the ``ecs`` endpoint at it.

    from oblako.engines.ecs_control import get_client
    ecs = get_client()  # boto3 ecs, pointed at the proxy
"""

from __future__ import annotations

import threading
import time

from oblako import ports
from oblako.engines.identity import claim_port, identify, is_engine

from .app import app, create_app

__all__ = ["app", "create_app", "get_client", "is_running", "start_in_thread"]

DEFAULT_PORT = ports.ECS_CONTROL

_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if the proxy is accepting requests on the port."""
    return is_engine(port, "ecs_control", timeout)


def start_in_thread(port: int = DEFAULT_PORT) -> str:
    """Start the ecs-control proxy in a daemon thread (idempotent). Returns its URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "ecs_control")
        config = uvicorn.Config(
            identify(create_app(), "ecs_control"),
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
    raise RuntimeError(f"ecs-control proxy did not start on port {port}")


def get_client(port: int = DEFAULT_PORT):
    """Return a boto3 ``ecs`` client wired to the proxy."""
    import os

    import boto3

    start_in_thread(port)
    return boto3.client(
        "ecs",
        endpoint_url=f"http://localhost:{port}",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    )
