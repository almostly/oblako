"""ECS task metadata endpoint for oblako's ECS tasks.

Real ECS/Fargate exposes a task metadata endpoint that a container reads via the
``ECS_CONTAINER_METADATA_URI`` (v3) / ``ECS_CONTAINER_METADATA_URI_V4`` env vars -
task ARN, family, the task's containers, network bindings, etc. oblako runs its
ECS task containers itself, so it serves the same endpoint here (on the host, so
no 169.254.170.2 link-local hack is needed - the URI is a plain URL). The metadata
is what oblako knows about the task from moto + the launched container; ``register``
is called by ``ECSService.run_task`` right after it starts each task.

    ECS_CONTAINER_METADATA_URI    = http://host.docker.internal:8011/<task>/<container>
    ECS_CONTAINER_METADATA_URI_V4 = http://host.docker.internal:8011/v4/<task>/<container>
    GET <uri>       -> that container's metadata
    GET <uri>/task  -> the whole task's metadata
"""

from __future__ import annotations

import threading
import time

from oblako import ports
from oblako.engines.identity import claim_port, identify, is_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

DEFAULT_PORT = ports.ECS_METADATA

# task_id -> {"task": <task metadata>, "containers": {name: <container metadata>}}
_TASKS: dict[str, dict] = {}
_lock = threading.Lock()
_servers: dict[int, object] = {}


def register(task_id: str, task_metadata: dict, containers: dict[str, dict]) -> None:
    """Record a running task's metadata so its containers can read it."""
    with _lock:
        _TASKS[task_id] = {"task": task_metadata, "containers": containers}


def deregister(task_id: str) -> None:
    """Drop a stopped task's metadata (idempotent)."""
    with _lock:
        _TASKS.pop(task_id, None)


def _container(task_id: str, container: str):
    with _lock:
        entry = _TASKS.get(task_id)
        return (
            dict(entry["containers"][container])
            if entry and container in entry["containers"]
            else None
        )


def _task(task_id: str):
    with _lock:
        entry = _TASKS.get(task_id)
        return dict(entry["task"]) if entry else None


async def _get_container(request: Request) -> JSONResponse:
    """GET /[v4/]{task}/{container} -> the container's metadata."""
    meta = _container(request.path_params["task"], request.path_params["container"])
    if meta is None:
        return JSONResponse({"error": "container metadata not found"}, status_code=404)
    return JSONResponse(meta)


async def _get_task(request: Request) -> JSONResponse:
    """GET /[v4/]{task}/{container}/task -> the whole task's metadata."""
    meta = _task(request.path_params["task"])
    if meta is None:
        return JSONResponse({"error": "task metadata not found"}, status_code=404)
    return JSONResponse(meta)


async def _empty_stats(_request: Request) -> JSONResponse:
    return JSONResponse({})


def create_app() -> Starlette:
    """Create the Starlette app serving v3 + v4 task metadata."""
    routes = []
    for prefix in ("", "/v4"):
        routes += [
            Route(prefix + "/{task}/{container}/task/stats", _empty_stats),
            Route(prefix + "/{task}/{container}/stats", _empty_stats),
            Route(prefix + "/{task}/{container}/task", _get_task),
            Route(prefix + "/{task}/{container}", _get_container),
        ]
    return Starlette(routes=routes)


app = create_app()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if the metadata server is reachable on the port."""
    return is_engine(port, "ecs_metadata", timeout)


def start_in_thread(port: int = DEFAULT_PORT) -> str:
    """Start the metadata server in a daemon thread (idempotent). Returns its URL."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "ecs_metadata")
        config = uvicorn.Config(
            identify(create_app(), "ecs_metadata"),
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
    raise RuntimeError(f"ecs metadata server did not start on port {port}")
