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

    def op_CreateModel(self, req: dict) -> Response:
        """Register a model."""
        if not req.get("ModelName"):
            return _error("ValidationException", "ModelName is required")
        return _json_response({"ModelArn": self.executor.create_model(req)})

    def op_CreateEndpointConfig(self, req: dict) -> Response:
        """Register an endpoint config."""
        if not req.get("EndpointConfigName"):
            return _error("ValidationException", "EndpointConfigName is required")
        return _json_response(
            {"EndpointConfigArn": self.executor.create_endpoint_config(req)}
        )

    def op_CreateEndpoint(self, req: dict) -> Response:
        """Start a local serving container for the endpoint."""
        if not req.get("EndpointName"):
            return _error("ValidationException", "EndpointName is required")
        return _json_response({"EndpointArn": self.executor.create_endpoint(req)})

    def op_DescribeEndpoint(self, req: dict) -> Response:
        """Return the current state of an endpoint."""
        endpoint = self.executor.describe_endpoint(_require(req, "EndpointName"))
        if endpoint is None:
            raise _NotFound(f"Endpoint {req.get('EndpointName')} not found")
        return _json_response(endpoint)

    def op_DeleteEndpoint(self, req: dict) -> Response:
        """Stop and remove an endpoint's serving container."""
        self.executor.delete_endpoint(_require(req, "EndpointName"))
        return _json_response({})

    def op_CreateTransformJob(self, req: dict) -> Response:
        """Start a local batch transform job."""
        if not req.get("TransformJobName"):
            return _error("ValidationException", "TransformJobName is required")
        return _json_response(
            {"TransformJobArn": self.executor.create_transform_job(req)}
        )

    def op_DescribeTransformJob(self, req: dict) -> Response:
        """Return the current state of a batch transform job."""
        job = self.executor.describe_transform_job(_require(req, "TransformJobName"))
        if job is None:
            raise _NotFound(f"Transform job {req.get('TransformJobName')} not found")
        return _json_response(job)

    def op_ListTransformJobs(self, req: dict) -> Response:
        """Return a summary list of all transform jobs."""
        return _json_response(
            {"TransformJobSummaries": self.executor.list_transform_jobs()}
        )

    async def invoke(self, request: Request) -> Response:
        """sagemaker-runtime InvokeEndpoint: proxy to the serving container."""
        from starlette.concurrency import run_in_threadpool

        name = request.path_params["name"]
        body = await request.body()
        content_type = request.headers.get("Content-Type", "application/octet-stream")
        try:
            result = await run_in_threadpool(
                self.executor.invoke_endpoint, name, body, content_type
            )
        except KeyError as err:
            return _error("ValidationError", str(err), status=404)
        except Exception as err:  # noqa: BLE001
            return _error("ModelError", str(err))
        return Response(result, media_type=content_type)


class _NotFound(Exception):
    pass


def _require(req: dict, key: str):
    if key not in req or req[key] is None:
        raise Exception(f"{key} is required")
    return req[key]


def create_app(executor: SageMakerExecutor | None = None) -> Starlette:
    """Create the Starlette app for the local sagemaker control-plane + runtime."""
    dispatcher = SageMakerApp(executor or SageMakerExecutor())
    return Starlette(
        routes=[
            # sagemaker (control plane): JSON 1.1, dispatched by X-Amz-Target
            Route("/", dispatcher.handle, methods=["POST"]),
            # sagemaker-runtime: REST, POST /endpoints/<name>/invocations
            Route(
                "/endpoints/{name}/invocations",
                dispatcher.invoke,
                methods=["POST"],
            ),
        ]
    )


app = create_app()
