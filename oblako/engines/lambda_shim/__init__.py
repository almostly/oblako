"""A minimal Lambda Invoke API so Step Functions tasks can call oblako services.

The local Step Functions engine (amazon/aws-stepfunctions-local) predates the
Bedrock service integration, but it *can* schedule ``lambda:invoke`` against its
``LAMBDA_ENDPOINT``. This server speaks just enough of the Lambda data-plane wire
protocol — ``POST /2015-03-31/functions/{name}/invocations`` — to let a state
machine's Task call a registered local "function". The ``bedrock-invoke`` function
forwards to the real local model (Ollama) via ``BedrockAdapter``, so a Bedrock
prompt-chain state machine runs end to end against your local model.

It binds 0.0.0.0 so the Step Functions container reaches it on
``host.docker.internal:3001`` (the service adds the ``host-gateway`` mapping).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

DEFAULT_PORT = 3001

_adapter = None
_adapter_lock = threading.Lock()
_servers: dict[int, object] = {}
_lock = threading.Lock()


def _get_adapter():
    """Build (once) the BedrockAdapter that talks to the local model backend."""
    global _adapter
    with _adapter_lock:
        if _adapter is None:
            from oblako.engines.bedrock.adapter import BedrockAdapter
            from oblako.engines.bedrock.backends import make_backend

            _adapter = BedrockAdapter(make_backend())
        return _adapter


def bedrock_invoke(event: dict) -> dict:
    """Call the local Bedrock model. event: {modelId?, prompt, context?} -> {text, modelId}."""
    model_id = event.get("modelId") or "qwen2.5:0.5b"
    prompt = event.get("prompt", "")
    context = event.get("context")
    if context:
        prompt = f"{context}\n\n{prompt}"
    result = _get_adapter().converse(
        model_id=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    text = result["output"]["message"]["content"][0]["text"]
    return {"text": text, "modelId": model_id}


# Registered local "functions" a state machine can invoke by name.
FUNCTIONS = {"bedrock-invoke": bedrock_invoke}


async def _invocations(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    handler = FUNCTIONS.get(name)
    if handler is None:
        return JSONResponse(
            {
                "errorMessage": f"Function not found: {name}",
                "errorType": "ResourceNotFoundException",
            },
            status_code=404,
            headers={"X-Amz-Function-Error": "Unhandled"},
        )
    try:
        event = json.loads(await request.body() or b"{}")
        return JSONResponse(handler(event))
    except Exception as e:  # noqa: BLE001 - surface as a Lambda function error
        return JSONResponse(
            {"errorMessage": str(e), "errorType": type(e).__name__},
            headers={"X-Amz-Function-Error": "Unhandled"},
        )


def create_app() -> Starlette:
    """Build the Starlette ASGI app exposing the Lambda Invoke route."""

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "functions": list(FUNCTIONS)})

    return Starlette(
        routes=[
            Route("/", health, methods=["GET"]),
            Route(
                "/2015-03-31/functions/{name}/invocations",
                _invocations,
                methods=["POST"],
            ),
        ]
    )


app = create_app()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if a lambda shim is reachable on the port."""
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_in_thread(port: int = DEFAULT_PORT) -> str:
    """Start the lambda shim in a daemon thread (idempotent). Binds 0.0.0.0."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        config = uvicorn.Config(
            create_app(), host="0.0.0.0", port=port, log_level="warning"
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
    raise RuntimeError(f"lambda shim did not start on port {port}")
