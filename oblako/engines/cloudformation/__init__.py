"""Local AWS CloudFormation over oblako's real engines.

Speaks the `cloudformation` wire protocol so a real boto3 client — and therefore
`aws cloudformation deploy` and `sam deploy` (point them at this with
AWS_ENDPOINT_URL_CLOUDFORMATION) — provisions actual oblako resources:

    import boto3
    cfn = boto3.client("cloudformation", endpoint_url="http://localhost:8017",
                       region_name="us-east-1",
                       aws_access_key_id="test", aws_secret_access_key="test")

Supported resource types map to oblako engines: AWS::S3::Bucket (S3Proxy),
AWS::DynamoDB::Table (DynamoDB Local), AWS::Redshift::Cluster and
AWS::RDS::DBInstance (control plane via moto).
"""

from __future__ import annotations

import threading
import time

from oblako import ports
from oblako.engines.identity import claim_port, identify, is_engine

from .app import app, create_app
from .engine import StackStore
from .providers import PROVIDERS

__all__ = [
    "app",
    "create_app",
    "StackStore",
    "PROVIDERS",
    "start_in_thread",
    "is_running",
]

DEFAULT_PORT = ports.CLOUDFORMATION

_servers: dict[int, "object"] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if a CloudFormation server is reachable on the port."""
    return is_engine(port, "cloudformation", timeout)


def start_in_thread(port: int = DEFAULT_PORT, store: StackStore | None = None) -> str:
    """Start the CloudFormation server in a daemon thread (idempotent).

    Returns the endpoint URL. Safe to call repeatedly; only starts once per port.
    """
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "cloudformation")
        application = create_app(store)
        config = uvicorn.Config(
            identify(application, "cloudformation"),
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
    raise RuntimeError(f"cloudformation server did not start on port {port}")
