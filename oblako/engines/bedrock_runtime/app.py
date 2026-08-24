"""ASGI app implementing the AWS Bedrock wire protocols (rest-json).

A single local endpoint serves both Bedrock services (boto3 routes by URI, so
one ``endpoint_url`` works for both clients):

  bedrock-runtime (data plane), translated to Ollama by ``BedrockAdapter``:
    POST /model/{modelId}/invoke    -> invoke_model
    POST /model/{modelId}/converse  -> converse

  bedrock (control plane):
    GET  /foundation-models                       -> ListFoundationModels
    GET  /foundation-models/{modelIdentifier}     -> GetFoundationModel
    POST /model-invocation-job                    -> CreateModelInvocationJob
    GET  /model-invocation-job/{jobIdentifier}    -> GetModelInvocationJob
    GET  /model-invocation-jobs                   -> ListModelInvocationJobs
    POST /model-invocation-job/{jobId}/stop       -> StopModelInvocationJob
"""

from __future__ import annotations

import base64
import datetime
import json
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from oblako.engines.bedrock import foundation_models
from oblako.engines.bedrock.adapter import BedrockAdapter
from oblako.engines.bedrock.backends import make_backend
from oblako.engines.bedrock.jobs import JobStore, new_job_details


def _jsonable(obj):
    if isinstance(obj, datetime.datetime):
        return obj.timestamp()  # rest-json timestamps are unix epoch
    raise TypeError(f"not JSON serializable: {type(obj)}")


def _json(payload: dict, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, default=_jsonable),
        status_code=status,
        media_type="application/json",
    )


def _error(
    message: str, error_type: str = "InternalServerException", status: int = 500
) -> Response:
    return JSONResponse(
        {"message": message, "__type": error_type},
        status_code=status,
        headers={"x-amzn-errortype": error_type},
    )


def _s3_factory(region: str):
    def factory():
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=os.environ.get("OBLAKO_S3_ENDPOINT", "http://localhost:9000"),
            region_name=region,
            aws_access_key_id="test",
            aws_secret_access_key="test",
            # S3Proxy doesn't implement the new default CRC32 checksums / aws-chunked.
            config=Config(
                signature_version="s3v4",
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    return factory


class BedrockRuntimeApp:
    """bedrock-runtime data plane."""

    def __init__(self, adapter: BedrockAdapter):
        """Initialize with a BedrockAdapter instance."""
        self.adapter = adapter

    async def invoke_model(self, request: Request) -> Response:
        """Handle POST /model/{modelId}/invoke and return a Bedrock invoke response."""
        model_id = request.path_params["model_id"]
        body = await request.body()
        try:
            result = self.adapter.invoke_model(model_id, body)
        except Exception as e:
            return _error(str(e), "ModelErrorException")
        return Response(json.dumps(result), media_type="application/json")

    async def invoke_model_with_response_stream(self, request: Request) -> Response:
        """POST /model/{modelId}/invoke-with-response-stream: a Bedrock event stream."""
        from starlette.responses import StreamingResponse

        from oblako.engines.bedrock.eventstream import encode_event

        model_id = request.path_params["model_id"]
        body = await request.body()

        def frames():
            try:
                for chunk in self.adapter.invoke_model_stream(model_id, body):
                    inner = json.dumps(chunk).encode()
                    yield encode_event(
                        "chunk", {"bytes": base64.b64encode(inner).decode()}
                    )
            except Exception as err:  # surface as a stream error event
                yield encode_event("internalServerException", {"message": str(err)})

        return StreamingResponse(
            frames(), media_type="application/vnd.amazon.eventstream"
        )

    async def converse_stream(self, request: Request) -> Response:
        """POST /model/{modelId}/converse-stream: a Bedrock Converse event stream."""
        from starlette.responses import StreamingResponse

        from oblako.engines.bedrock.eventstream import encode_event

        model_id = request.path_params["model_id"]
        try:
            req = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            return _error("Invalid JSON body", "ValidationException", 400)

        def frames():
            try:
                for event_type, payload in self.adapter.converse_stream(
                    model_id=model_id,
                    messages=req.get("messages", []),
                    system=req.get("system"),
                    inference_config=req.get("inferenceConfig"),
                ):
                    yield encode_event(event_type, payload)
            except Exception as err:
                yield encode_event("internalServerException", {"message": str(err)})

        return StreamingResponse(
            frames(), media_type="application/vnd.amazon.eventstream"
        )

    async def converse(self, request: Request) -> Response:
        """Handle POST /model/{modelId}/converse and return a Bedrock Converse response."""
        model_id = request.path_params["model_id"]
        try:
            req = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            return _error("Invalid JSON body", "ValidationException", 400)
        try:
            result = self.adapter.converse(
                model_id=model_id,
                messages=req.get("messages", []),
                system=req.get("system"),
                inference_config=req.get("inferenceConfig"),
            )
        except Exception as e:
            return _error(str(e), "ModelErrorException")
        return JSONResponse(result)


class BedrockControlApp:
    """bedrock control plane: foundation models + batch model-invocation jobs."""

    def __init__(self, adapter: BedrockAdapter, region: str = "us-east-1"):
        """Initialize with a BedrockAdapter and AWS region."""
        self.adapter = adapter
        self.region = region
        self.jobs = JobStore()
        self.s3_factory = _s3_factory(region)

    # -- foundation models --------------------------------------------------
    async def list_foundation_models(self, request: Request) -> Response:
        """Handle GET /foundation-models and return catalog summaries plus live backend models."""
        summaries = foundation_models.list_models(self.region)
        try:  # also surface the backend's live models (Ollama tags / OpenRouter slugs)
            for m in self.adapter.backend.list_models():
                summaries.append(
                    foundation_models.live_model_summary(
                        m["modelId"], m["providerName"], self.region
                    )
                )
        except Exception:  # engine may be down; static catalog still returned
            pass
        return _json({"modelSummaries": summaries})

    async def get_foundation_model(self, request: Request) -> Response:
        """Handle GET /foundation-models/{modelIdentifier} and return model details."""
        model_id = request.path_params["model_identifier"]
        detail = foundation_models.get_model(model_id, self.region)
        if detail is None:
            return _error(
                "The provided model identifier is invalid.", "ValidationException", 400
            )
        return _json({"modelDetails": detail})

    # -------------------------------------------------------------------------------
    # Model-invocation jobs
    # -------------------------------------------------------------------------------
    async def create_job(self, request: Request) -> Response:
        """Handle POST /model-invocation-job, create a batch job, and return its ARN."""
        try:
            req = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            return _error("Invalid JSON body", "ValidationException", 400)
        required = (
            "jobName",
            "roleArn",
            "modelId",
            "inputDataConfig",
            "outputDataConfig",
        )
        missing = [k for k in required if k not in req]
        if missing:
            return _error(
                f"Missing required field(s): {', '.join(missing)}",
                "ValidationException",
                400,
            )
        details = new_job_details(
            job_name=req["jobName"],
            model_id=req["modelId"],
            role_arn=req["roleArn"],
            input_config=req["inputDataConfig"],
            output_config=req["outputDataConfig"],
            region=self.region,
            client_request_token=req.get("clientRequestToken"),
            timeout_hours=req.get("timeoutDurationInHours"),
        )
        job = self.jobs.create(details, self.adapter, self.s3_factory)
        job.start()
        return _json({"jobArn": details["jobArn"]})

    async def get_job(self, request: Request) -> Response:
        """Handle GET /model-invocation-job/{jobIdentifier} and return job details."""
        job_id = request.path_params["job_identifier"].split("/")[-1]
        job = self.jobs.get(job_id)
        if job is None:
            return _error(
                "The provided job identifier is invalid.", "ValidationException", 400
            )
        return _json(job.details)

    async def list_jobs(self, request: Request) -> Response:
        """Handle GET /model-invocation-jobs and return summaries of all jobs."""
        return _json({"invocationJobSummaries": [j.details for j in self.jobs.list()]})

    async def stop_job(self, request: Request) -> Response:
        """Handle POST /model-invocation-job/{jobIdentifier}/stop and signal the job to stop."""
        job_id = request.path_params["job_identifier"].split("/")[-1]
        job = self.jobs.get(job_id)
        if job is None:
            return _error(
                "The provided job identifier is invalid.", "ValidationException", 400
            )
        job.stop()
        return _json({})


def create_app(
    adapter: BedrockAdapter | None = None,
    ollama_url: str | None = None,
    region: str = "us-east-1",
) -> Starlette:
    """Build and return the Starlette ASGI app wiring runtime and control plane routes."""
    if adapter is None:
        adapter = BedrockAdapter(make_backend(ollama_url=ollama_url))
    runtime = BedrockRuntimeApp(adapter)
    control = BedrockControlApp(adapter, region=region)

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/", health, methods=["GET"]),
            # bedrock-runtime
            Route(
                "/model/{model_id:path}/invoke", runtime.invoke_model, methods=["POST"]
            ),
            Route(
                "/model/{model_id:path}/converse", runtime.converse, methods=["POST"]
            ),
            Route(
                "/model/{model_id:path}/invoke-with-response-stream",
                runtime.invoke_model_with_response_stream,
                methods=["POST"],
            ),
            Route(
                "/model/{model_id:path}/converse-stream",
                runtime.converse_stream,
                methods=["POST"],
            ),
            # bedrock control plane
            Route(
                "/foundation-models", control.list_foundation_models, methods=["GET"]
            ),
            Route(
                "/foundation-models/{model_identifier:path}",
                control.get_foundation_model,
                methods=["GET"],
            ),
            Route("/model-invocation-jobs", control.list_jobs, methods=["GET"]),
            Route("/model-invocation-job", control.create_job, methods=["POST"]),
            Route(
                "/model-invocation-job/{job_identifier:path}/stop",
                control.stop_job,
                methods=["POST"],
            ),
            Route(
                "/model-invocation-job/{job_identifier:path}",
                control.get_job,
                methods=["GET"],
            ),
        ]
    )


# Module-level app for `uvicorn oblako.bedrock_runtime.app:app`
app = create_app()
