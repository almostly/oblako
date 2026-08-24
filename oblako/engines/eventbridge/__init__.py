"""EventBridge over moto that actually fires rule targets (incl. Redshift Data).

moto stores rules/targets but never delivers them; this in-process proxy forwards
the control plane to moto and, on PutEvents (and on ScheduleExpression rules),
matches rules and delivers to their targets - SQS / SNS / Lambda / Redshift Data.

    from oblako.engines.eventbridge import get_client
    ev = get_client()  # boto3 events, pointed at the proxy
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

from oblako import ports

from .app import EventBridgeProxy, app, create_app

__all__ = [
    "app",
    "create_app",
    "EventBridgeProxy",
    "start_in_thread",
    "is_running",
    "get_client",
]

DEFAULT_PORT = ports.EVENTBRIDGE

_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if the proxy is accepting requests on the port."""
    req = urllib.request.Request(
        f"http://localhost:{port}/",
        data=b"{}",
        method="POST",
        headers={"X-Amz-Target": "AWSEvents.ListRules"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def start_in_thread(port: int = DEFAULT_PORT, backend_url: str | None = None) -> str:
    """Start the eventbridge proxy in a daemon thread (idempotent). Returns its URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        config = uvicorn.Config(
            create_app(backend_url), host="127.0.0.1", port=port, log_level="warning"
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
    raise RuntimeError(f"eventbridge proxy did not start on port {port}")


def get_client(port: int = DEFAULT_PORT, backend_url: str | None = None):
    """Return a boto3 ``events`` client wired to the local proxy."""
    import os

    import boto3

    start_in_thread(port, backend_url)
    return boto3.client(
        "events",
        endpoint_url=f"http://localhost:{port}",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    )
