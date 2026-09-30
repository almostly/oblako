"""Local AWS AppConfig — control plane + data plane + the rule-evaluation agent.

A real boto3 client works against oblako:

    import boto3
    ac = boto3.client("appconfig", endpoint_url="http://localhost:8003",
                      region_name="us-east-1",
                      aws_access_key_id="test", aws_secret_access_key="test")
    app = ac.create_application(Name="demo-app")

And the bundled agent (a Python port of the AWS AppConfig Lambda extension) does
feature-flag rule + variant evaluation with request context:

    from oblako.engines.appconfig import AppConfigClient
    agent = AppConfigClient(endpoint_url="http://localhost:8003")
    flags = agent.evaluate("demo-app", "dev", "feature-flags", context={"tier": "vip"})
"""

from __future__ import annotations

import threading
import time

from oblako import ports
from oblako.engines.identity import claim_port, identify, is_engine
from .agent import AppConfigClient
from .app import app, create_app
from .engine import AppConfigError, AppConfigStore
from .rule_evaluator import evaluate_config, evaluate_rule, extract_attributes

__all__ = [
    "app",
    "create_app",
    "AppConfigStore",
    "AppConfigError",
    "AppConfigClient",
    "evaluate_config",
    "evaluate_rule",
    "extract_attributes",
    "start_in_thread",
    "is_running",
    "DEFAULT_PORT",
]

DEFAULT_PORT = ports.APPCONFIG

_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if an AppConfig server is reachable on the port."""
    return is_engine(port, "appconfig", timeout)


def start_in_thread(
    port: int = DEFAULT_PORT, store: AppConfigStore | None = None
) -> str:
    """Start the AppConfig server in a daemon thread (idempotent). Returns its URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "appconfig")
        application = create_app(store)
        config = uvicorn.Config(
            identify(application, "appconfig"),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        _servers[port] = server

    deadline = time.time() + 10
    while time.time() < deadline:
        if is_running(port):
            return url
        time.sleep(0.1)
    raise RuntimeError(f"AppConfig server did not start on port {port}")
