"""Local AWS SageMaker control plane over local Docker execution.

Exposes the ``sagemaker`` wire protocol so a real boto3 client works:

    import boto3
    sm = boto3.client("sagemaker", endpoint_url="http://localhost:8005",
                      region_name="us-east-1",
                      aws_access_key_id="test", aws_secret_access_key="test")
    sm.create_training_job(...)
    sm.describe_training_job(TrainingJobName=...)
"""

from __future__ import annotations

import threading
import time
import urllib.request

from .app import app, create_app
from .executor import SageMakerExecutor
from .stubs import use_local_stubs

__all__ = [
    "app",
    "create_app",
    "SageMakerExecutor",
    "use_local_stubs",
    "start_in_thread",
    "is_running",
]

DEFAULT_PORT = 8005

_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if a sagemaker server is reachable on the port."""
    req = urllib.request.Request(
        f"http://localhost:{port}/",
        data=b"{}",
        method="POST",
        headers={"X-Amz-Target": "SageMaker.ListTrainingJobs"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_in_thread(
    port: int = DEFAULT_PORT, executor: SageMakerExecutor | None = None
) -> str:
    """Start the sagemaker server in a daemon thread (idempotent). Returns its URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        application = create_app(executor)
        config = uvicorn.Config(
            application, host="127.0.0.1", port=port, log_level="warning"
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
    raise RuntimeError(f"sagemaker server did not start on port {port}")
