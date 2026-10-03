"""ECS API proxy over moto that runs tasks as real containers.

moto serves the ECS control plane (clusters, task definitions, services) but
cannot run tasks: RunTask without container instances fails. This proxy
forwards every call to moto except the task operations, which it answers from
oblako's ECS runner (``oblako.services.ecs``):

* ``RunTask`` starts the task definition's containers on Docker;
* ``DescribeTasks`` reports their real state, with each container's exit code;
* ``ListTasks`` and ``StopTask`` work on those containers.

So unmodified boto3 (``ecs.run_task`` and the ``tasks_stopped`` waiter) runs a
task locally as on Fargate.
"""

from __future__ import annotations

import json
import os

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from oblako import ports

TARGET = "AmazonEC2ContainerServiceV20141113."
HANDLED = {"RunTask", "DescribeTasks", "ListTasks", "StopTask"}


def _moto_url() -> str:
    return os.environ.get("OBLAKO_MOTO_ENDPOINT") or f"http://localhost:{ports.MOTO}"


def _cluster_name(value: str | None) -> str:
    """Accept a cluster name or ARN, as ECS does."""
    return (value or "default").rsplit("/", 1)[-1]


def _ecs():
    from oblako.services import Oblako

    return Oblako().ecs


def _error(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"__type": code, "message": message},
        status_code=status,
        media_type="application/x-amz-json-1.1",
    )


def handle_task_call(operation: str, req: dict) -> dict:
    """Answer one task operation from oblako's ECS runner."""
    ecs = _ecs()
    cluster = _cluster_name(req.get("cluster"))
    if operation == "RunTask":
        run = ecs.run_task(
            req["taskDefinition"],
            cluster=cluster,
            count=int(req.get("count", 1)),
            launch_type=req.get("launchType", "FARGATE"),
            network_configuration=req.get("networkConfiguration"),
        )
        return run
    if operation == "DescribeTasks":
        return ecs.describe_tasks(cluster=cluster, tasks=req.get("tasks") or [])
    if operation == "ListTasks":
        arns = ecs.list_tasks(cluster=cluster)
        wanted = req.get("desiredStatus")
        if wanted:
            described = ecs.describe_tasks(cluster=cluster, tasks=arns)["tasks"]
            arns = [t["taskArn"] for t in described if t["desiredStatus"] == wanted]
        return {"taskArns": arns}
    # StopTask
    described = ecs.describe_tasks(cluster=cluster, tasks=[req["task"]])["tasks"]
    ecs.stop_task(req["task"])
    task = described[0] if described else {"taskArn": req["task"]}
    return {"task": {**task, "lastStatus": "STOPPED", "desiredStatus": "STOPPED"}}


async def handle(request: Request) -> Response:
    """Answer task operations; forward everything else to moto."""
    body = await request.body()
    target = request.headers.get("x-amz-target", "")
    operation = target.removeprefix(TARGET)
    if operation in HANDLED:
        try:
            req = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return _error("InvalidParameterException", "Invalid JSON body")
        try:
            result = await run_in_threadpool(handle_task_call, operation, req)
        except KeyError as e:
            return _error("InvalidParameterException", f"{e.args[0]} is required")
        except Exception as e:  # moto refused the task definition, Docker failed
            return _error("ClientException", str(e))
        return JSONResponse(result, media_type="application/x-amz-json-1.1")
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    async with httpx.AsyncClient(timeout=60) as client:
        upstream = await client.request(
            request.method,
            _moto_url() + request.url.path,
            content=body,
            headers=headers,
        )
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


def create_app() -> Starlette:
    """Build the proxy's ASGI app."""
    return Starlette(routes=[Route("/{path:path}", handle, methods=["POST", "GET"])])


app = create_app()
