"""DynamoDB native vector search over DynamoDB Local.

AWS added native vector search to DynamoDB (``SearchVectors`` + ``VectorIndexes``);
DynamoDB Local doesn't implement it, so oblako runs a thin proxy in front that
captures the vector indexes and serves ``SearchVectors`` as brute-force KNN. A
client-side model graft (``enable_dynamodb_vectors``) lets an otherwise-unpatched
boto3 form the new requests:

    from oblako.engines.dynamodb_vectors import get_client
    ddb = get_client()  # boto3 dynamodb, vector API enabled, pointed at the proxy
    ddb.create_table(..., VectorIndexes=[...])
    ddb.search_vectors(TableName=..., IndexName=..., SearchVector=[...], TopK=5)
"""

from __future__ import annotations

import contextlib
import threading
import time

from oblako import ports
from oblako.engines.identity import claim_port, identify, is_engine

from .app import app, create_app
from .model import enable_dynamodb_vectors

__all__ = [
    "app",
    "create_app",
    "enable_dynamodb_vectors",
    "start_in_thread",
    "is_running",
    "get_client",
]

DEFAULT_PORT = ports.DYNAMODB_VECTORS

_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if the proxy is accepting requests on the port."""
    return is_engine(port, "dynamodb_vectors", timeout)


def start_in_thread(port: int = DEFAULT_PORT, backend_url: str | None = None) -> str:
    """Start the vector proxy in a daemon thread (idempotent). Returns its URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "dynamodb_vectors")
        application = create_app(backend_url)
        config = uvicorn.Config(
            identify(application, "dynamodb_vectors"),
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
    raise RuntimeError(f"dynamodb vector proxy did not start on port {port}")


def get_client(port: int = DEFAULT_PORT, backend_url: str | None = None):
    """Return a boto3 dynamodb client with the vector API enabled, via the proxy.

    The caller must have DynamoDB Local running (the proxy forwards to it);
    ``DynamoDBService().get_vector_client()`` starts it for you.
    """
    import os

    import boto3

    data_dir = enable_dynamodb_vectors()
    start_in_thread(port, backend_url)
    # a fresh Session has its own loader with an empty model cache, so it loads
    # the augmented dynamodb model even if a plain client was built earlier
    session = boto3.Session()
    if data_dir:  # None when a current botocore already ships the real model
        with contextlib.suppress(Exception):
            session._session.get_component("data_loader").search_paths.insert(
                0, data_dir
            )
    return session.client(
        "dynamodb",
        endpoint_url=f"http://localhost:{port}",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    )
