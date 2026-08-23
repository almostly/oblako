"""ASGI app implementing the AWS ``sagemaker`` control-plane wire protocol.

boto3's ``sagemaker`` client talks to this via ``endpoint_url``. Each request is a
POST with ``X-Amz-Target: SageMaker.<Operation>`` and a JSON body. Training jobs
execute locally in Docker (see ``SageMakerExecutor``). The ``sagemaker-runtime``
invoke path (REST, not JSON-target) is added in a later increment.
"""

from __future__ import annotations

import datetime
import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .executor import SageMakerExecutor


def _jsonable(obj):
    """JSON-encode, converting datetimes to unix epoch (json protocol timestamps)."""
    if isinstance(obj, datetime.datetime):
        return obj.timestamp()
    raise TypeError(f"not JSON serializable: {type(obj)}")


def _json_response(
    payload: dict, status: int = 200, error_type: str | None = None
) -> Response:
    """Return an ``application/x-amz-json-1.1`` response."""
    headers = {"Content-Type": "application/x-amz-json-1.1"}
    if error_type:
        headers["X-Amzn-Errortype"] = error_type
    return Response(
        json.dumps(payload, default=_jsonable), status_code=status, headers=headers
    )


def _error(code: str, message: str, status: int = 400) -> Response:
    return _json_response(
        {"__type": code, "message": message}, status=status, error_type=code
    )


class SageMakerApp:
    """Dispatches sagemaker control-plane operations to a ``SageMakerExecutor``."""

    def __init__(self, executor: SageMakerExecutor):
        """Bind the dispatcher to the given executor."""
        self.executor = executor

    async def handle(self, request: Request) -> Response:
        """Dispatch an incoming request to the appropriate operation handler."""
        op = request.headers.get("X-Amz-Target", "").split(".")[-1]
        try:
            body = await request.body()
            req = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _error("ValidationException", "Invalid JSON body")
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            return _error("ValidationException", f"Unknown operation: {op or '(none)'}")
        try:
            return handler(req)
        except _NotFound as err:
            return _error("ResourceNotFound", str(err), status=400)
        except Exception as err:  # noqa: BLE001 - surface as ValidationException
            return _error("ValidationException", str(err))

    def op_CreateTrainingJob(self, req: dict) -> Response:
        """Start a local training job; return its ARN."""
        if not req.get("TrainingJobName"):
            return _error("ValidationException", "TrainingJobName is required")
        arn = self.executor.create_training_job(req)
        return _json_response({"TrainingJobArn": arn})

    def op_DescribeTrainingJob(self, req: dict) -> Response:
        """Return the current state of a training job."""
        job = self.executor.describe_training_job(_require(req, "TrainingJobName"))
        if job is None:
            raise _NotFound(f"Training job {req.get('TrainingJobName')} not found")
        return _json_response(job)

    def op_ListTrainingJobs(self, req: dict) -> Response:
        """Return a summary list of all training jobs."""
        return _json_response(
            {"TrainingJobSummaries": self.executor.list_training_jobs()}
        )


class _NotFound(Exception):
    pass


def _require(req: dict, key: str):
    if key not in req or req[key] is None:
        raise Exception(f"{key} is required")
    return req[key]


def create_app(executor: SageMakerExecutor | None = None) -> Starlette:
    """Create the Starlette app for the local sagemaker control-plane."""
    dispatcher = SageMakerApp(executor or SageMakerExecutor())
    return Starlette(routes=[Route("/", dispatcher.handle, methods=["POST"])])


app = create_app()
