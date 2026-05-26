"""Local AWS Bedrock Runtime over the Ollama engine.

Exposes the `bedrock-runtime` wire protocol so a real boto3 client works:

    import boto3, json
    br = boto3.client("bedrock-runtime", endpoint_url="http://localhost:8004",
                      region_name="us-east-1",
                      aws_access_key_id="test", aws_secret_access_key="test")
    br.converse(modelId="qwen2.5:0.5b",
                messages=[{"role": "user", "content": [{"text": "Hi"}]}])
"""

from __future__ import annotations

import threading
import time
import urllib.request

from .app import app, create_app

__all__ = ["app", "create_app", "start_in_thread", "is_running"]

DEFAULT_PORT = 8004

_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if a bedrock-runtime server is reachable on the port."""
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_in_thread(port: int = DEFAULT_PORT, ollama_url: str | None = None) -> str:
    """Start the bedrock-runtime server in a daemon thread (idempotent)."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        application = create_app(ollama_url=ollama_url)
        config = uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        _servers[port] = server

    deadline = time.time() + 10
    while time.time() < deadline:
        if is_running(port):
            return url
        time.sleep(0.1)
    raise RuntimeError(f"bedrock-runtime server did not start on port {port}")
