"""The Amazon MWAA API (rest-json) over oblako's Airflow environments.

boto3 prefixes MWAA's hostnames (``api.`` for the control plane, ``env.`` for
InvokeRestApi), so a client pointed at ``http://localhost:8016`` calls
``api.localhost:8016`` and ``env.localhost:8016``. Both resolve to the loopback
address on macOS and on Linux hosts with systemd-resolved; the engine listens on
IPv4 and IPv6 because macOS resolves them to ``::1``.
"""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import environments as envs

# fields of a record that are oblako's own, not the API's
_PRIVATE = {"port", "fernet_key", "dag_etags", "error"}


def _error(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"message": message},
        status_code=status,
        headers={"x-amzn-errortype": code},
    )


def _view(record: dict) -> dict:
    """Return the record as GetEnvironment's Environment shape."""
    out = {k: v for k, v in record.items() if k not in _PRIVATE and v is not None}
    for key in ("CreatedAt",):
        if isinstance(out.get(key), str):
            out[key] = datetime.fromisoformat(out[key]).timestamp()
    if record.get("error"):
        out["LastUpdate"] = {
            "Status": "FAILED",
            "Error": {"ErrorCode": "OblakoError", "ErrorMessage": record["error"]},
        }
    return out


async def _body(request: Request) -> dict:
    raw = await request.body()
    return json.loads(raw) if raw else {}


async def _call(fn, *args):
    try:
        return await run_in_threadpool(fn, *args)
    except envs.MwaaError as e:
        return _error(e.code, e.message, e.status)


async def environments(request: Request) -> Response:
    """CreateEnvironment, GetEnvironment, UpdateEnvironment, DeleteEnvironment."""
    name = request.path_params["name"]
    if request.method == "PUT":
        result = await _call(envs.create, name, await _body(request))
        if isinstance(result, Response):
            return result
        return JSONResponse({"Arn": result["Arn"]})
    if request.method == "PATCH":
        result = await _call(envs.update, name, await _body(request))
        if isinstance(result, Response):
            return result
        return JSONResponse({"Arn": result["Arn"]})
    if request.method == "DELETE":
        result = await _call(envs.delete, name)
        return result if isinstance(result, Response) else JSONResponse({})
    record = envs.get(name)
    if record is None:
        return _error("ResourceNotFoundException", f"Environment {name} not found", 404)
    return JSONResponse({"Environment": _view(record)})


async def list_environments(request: Request) -> Response:
    """ListEnvironments."""
    return JSONResponse({"Environments": sorted(envs.load())})


async def invoke_rest_api(request: Request) -> Response:
    """InvokeRestApi: Airflow's REST API, with AWS's exceptions for its errors."""
    name = request.path_params["name"]
    result = await _call(envs.invoke_rest_api, name, await _body(request))
    if isinstance(result, Response):
        return result
    status, body = result
    payload = {"RestApiStatusCode": status, "RestApiResponse": body}
    if status >= 500:
        return JSONResponse(
            payload,
            status_code=400,
            headers={"x-amzn-errortype": "RestApiServerException"},
        )
    if status >= 400:
        return JSONResponse(
            payload,
            status_code=400,
            headers={"x-amzn-errortype": "RestApiClientException"},
        )
    return JSONResponse(payload)


async def tags(request: Request) -> Response:
    """TagResource, UntagResource, ListTagsForResource."""
    resource = unquote(request.path_params["arn"])
    name = resource.rsplit("/", 1)[-1]
    record = envs.get(name)
    if record is None or record["Arn"] != resource:
        return _error(
            "ResourceNotFoundException", f"Resource {resource} not found", 404
        )
    current = dict(record.get("Tags") or {})
    if request.method == "POST":
        current.update((await _body(request)).get("Tags", {}))
        envs.set_tags(name, current)
        return JSONResponse({})
    if request.method == "DELETE":
        for key in request.query_params.getlist("tagKeys"):
            current.pop(key, None)
        envs.set_tags(name, current)
        return JSONResponse({})
    return JSONResponse({"Tags": current})


async def unsupported(request: Request) -> Response:
    """CreateCliToken, CreateWebLoginToken and PublishMetrics are not simulated."""
    return _error(
        "ValidationException",
        "oblako's MWAA does not simulate this operation; open the webserver "
        "(WebserverUrl) or use InvokeRestApi",
    )


def create_app() -> Starlette:
    """Build the MWAA API app; DAG sync runs while it serves."""
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app):
        thread = threading.Thread(target=envs.sync_loop, args=(stop,), daemon=True)
        thread.start()
        yield
        stop.set()

    return Starlette(
        routes=[
            Route("/environments", list_environments, methods=["GET"]),
            Route(
                "/environments/{name}",
                environments,
                methods=["GET", "PUT", "PATCH", "DELETE"],
            ),
            Route("/restapi/{name}", invoke_rest_api, methods=["POST"]),
            Route("/tags/{arn:path}", tags, methods=["GET", "POST", "DELETE"]),
            Route("/clitoken/{name}", unsupported, methods=["POST"]),
            Route("/webtoken/{name}", unsupported, methods=["POST"]),
            Route("/metrics/environments/{name}", unsupported, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


app = create_app()
