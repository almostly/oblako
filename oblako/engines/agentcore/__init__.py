"""Local Amazon Bedrock AgentCore Runtime.

The AgentCore Runtime contract is just an ASGI app exposing ``POST /invocations``
and ``GET /ping`` on port 8080 — the same contract it uses in the cloud. The
`bedrock-agentcore` SDK provides ``BedrockAgentCoreApp`` for exactly this, so an
agent runs locally with no cloud at all. Pair it with oblako's local Bedrock
(Ollama) for a fully offline agent loop.

    pip install 'oblako[agentcore]'

    from oblako.engines.agentcore import BedrockAgentCoreApp
    app = BedrockAgentCoreApp()

    @app.entrypoint
    def handler(payload):
        return {"reply": ...}

Run / invoke it via the CLI:

    oblako agentcore run my_agent.py        # serves /invocations + /ping on 8080
    oblako agentcore invoke '{"prompt": "hi"}'
"""

from __future__ import annotations

import importlib.util
import json
import urllib.request

__all__ = ["BedrockAgentCoreApp", "run", "invoke", "is_running"]

DEFAULT_PORT = 8080

_INSTALL_HINT = (
    "bedrock-agentcore is not installed. Install the AgentCore extra:\n"
    "    pip install 'oblako[agentcore]'   (or: uv pip install bedrock-agentcore)"
)


def _agentcore_app_class():
    """Lazily import BedrockAgentCoreApp with a friendly error if missing."""
    try:
        from bedrock_agentcore.runtime import BedrockAgentCoreApp as _App
    except ImportError as e:  # pragma: no cover - depends on optional extra
        raise ImportError(_INSTALL_HINT) from e
    return _App


def __getattr__(name: str):
    """Lazily re-export ``BedrockAgentCoreApp`` (imported only on use)."""
    # Lazy re-export so `from oblako.engines.agentcore import BedrockAgentCoreApp` works
    # without importing the optional dependency until it's actually used.
    if name == "BedrockAgentCoreApp":
        return _agentcore_app_class()
    raise AttributeError(name)


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if an AgentCore runtime is reachable (GET /ping)."""
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/ping", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def invoke(payload: dict, port: int = DEFAULT_PORT, timeout: float = 120.0) -> dict:
    """POST a payload to a running agent's /invocations endpoint."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://localhost:{port}/invocations",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def run(entrypoint: str, port: int = DEFAULT_PORT, host: str = "0.0.0.0") -> None:
    """Load an agent file and serve it (blocks).

    The file must define a module-level ``BedrockAgentCoreApp`` instance.
    """
    app_class = _agentcore_app_class()
    spec = importlib.util.spec_from_file_location("oblako_agent", entrypoint)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"Cannot load agent file: {entrypoint}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    app = next(
        (obj for obj in vars(module).values() if isinstance(obj, app_class)), None
    )
    if app is None:
        raise ValueError(
            f"{entrypoint} does not define a BedrockAgentCoreApp instance "
            "(expected something like `app = BedrockAgentCoreApp()`)."
        )
    print(
        f"Serving AgentCore agent on http://localhost:{port}  (POST /invocations, GET /ping)"
    )
    app.run(port=port, host=host)
