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


def _rest_json(
    payload: dict, status: int = 200, error_type: str | None = None
) -> Response:
    """Return an ``application/json`` response for the rest-json featurestore-runtime."""
    body = dict(payload)
    headers = {"Content-Type": "application/json"}
    if error_type:
        body = {"__type": error_type, **body}
        headers["X-Amzn-Errortype"] = error_type
    return Response(
        json.dumps(body, default=_jsonable), status_code=status, headers=headers
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

    def op_CreateProcessingJob(self, req: dict) -> Response:
        """Start a local processing job."""
        if not req.get("ProcessingJobName"):
            return _error("ValidationException", "ProcessingJobName is required")
        return _json_response(
            {"ProcessingJobArn": self.executor.create_processing_job(req)}
        )

    def op_DescribeProcessingJob(self, req: dict) -> Response:
        """Return the current state of a processing job."""
        job = self.executor.describe_processing_job(_require(req, "ProcessingJobName"))
        if job is None:
            raise _NotFound(f"Processing job {req.get('ProcessingJobName')} not found")
        return _json_response(job)

    def op_ListProcessingJobs(self, req: dict) -> Response:
        """Return a summary list of all processing jobs."""
        return _json_response(
            {"ProcessingJobSummaries": self.executor.list_processing_jobs()}
        )

    def op_CreateHyperParameterTuningJob(self, req: dict) -> Response:
        """Start a local hyperparameter tuning (HPO) job."""
        if not req.get("HyperParameterTuningJobName"):
            return _error(
                "ValidationException", "HyperParameterTuningJobName is required"
            )
        arn = self.executor.create_hyper_parameter_tuning_job(req)
        return _json_response({"HyperParameterTuningJobArn": arn})

    def op_DescribeHyperParameterTuningJob(self, req: dict) -> Response:
        """Return the current state of a tuning job (incl. BestTrainingJob)."""
        job = self.executor.describe_hyper_parameter_tuning_job(
            _require(req, "HyperParameterTuningJobName")
        )
        if job is None:
            raise _NotFound(
                f"Tuning job {req.get('HyperParameterTuningJobName')} not found"
            )
        return _json_response(job)

    def op_ListHyperParameterTuningJobs(self, req: dict) -> Response:
        """Return a summary list of all tuning jobs."""
        return _json_response(
            {
                "HyperParameterTuningJobSummaries": (
                    self.executor.list_hyper_parameter_tuning_jobs()
                )
            }
        )

    def op_DescribeModel(self, req: dict) -> Response:
        """Return a model record."""
        model = self.executor.describe_model(_require(req, "ModelName"))
        if model is None:
            raise _NotFound(f"Model {req.get('ModelName')} not found")
        return _json_response(model)

    def op_ListModels(self, req: dict) -> Response:
        """Return a summary list of all models."""
        return _json_response({"Models": self.executor.list_models()})

    def op_DeleteModel(self, req: dict) -> Response:
        """Delete a model registration."""
        self.executor.delete_model(_require(req, "ModelName"))
        return _json_response({})

    def op_DescribeEndpointConfig(self, req: dict) -> Response:
        """Return an endpoint config record."""
        config = self.executor.describe_endpoint_config(
            _require(req, "EndpointConfigName")
        )
        if config is None:
            raise _NotFound(f"EndpointConfig {req.get('EndpointConfigName')} not found")
        return _json_response(config)

    def op_ListEndpointConfigs(self, req: dict) -> Response:
        """Return a summary list of all endpoint configs."""
        return _json_response(
            {"EndpointConfigs": self.executor.list_endpoint_configs()}
        )

    def op_DeleteEndpointConfig(self, req: dict) -> Response:
        """Delete an endpoint config."""
        self.executor.delete_endpoint_config(_require(req, "EndpointConfigName"))
        return _json_response({})

    def op_ListEndpoints(self, req: dict) -> Response:
        """Return a summary list of all endpoints."""
        return _json_response({"Endpoints": self.executor.list_endpoints()})

    def op_AddTags(self, req: dict) -> Response:
        """Attach tags to a resource; return the resulting tag set."""
        tags = self.executor.add_tags(
            _require(req, "ResourceArn"), req.get("Tags", [])
        )
        return _json_response({"Tags": tags})

    def op_DeleteTags(self, req: dict) -> Response:
        """Remove tag keys from a resource."""
        self.executor.delete_tags(_require(req, "ResourceArn"), req.get("TagKeys", []))
        return _json_response({})

    def op_ListTags(self, req: dict) -> Response:
        """Return the tags attached to a resource."""
        return _json_response(
            {"Tags": self.executor.list_tags(_require(req, "ResourceArn"))}
        )

    def op_StopTrainingJob(self, req: dict) -> Response:
        """Request a training job stop."""
        if not self.executor.stop_training_job(_require(req, "TrainingJobName")):
            raise _NotFound(f"Training job {req.get('TrainingJobName')} not found")
        return _json_response({})

    def op_StopTransformJob(self, req: dict) -> Response:
        """Request a batch transform job stop."""
        if not self.executor.stop_transform_job(_require(req, "TransformJobName")):
            raise _NotFound(f"Transform job {req.get('TransformJobName')} not found")
        return _json_response({})

    def op_StopProcessingJob(self, req: dict) -> Response:
        """Request a processing job stop."""
        if not self.executor.stop_processing_job(_require(req, "ProcessingJobName")):
            raise _NotFound(f"Processing job {req.get('ProcessingJobName')} not found")
        return _json_response({})

    def op_StopHyperParameterTuningJob(self, req: dict) -> Response:
        """Request a tuning job stop."""
        if not self.executor.stop_hyper_parameter_tuning_job(
            _require(req, "HyperParameterTuningJobName")
        ):
            raise _NotFound(
                f"Tuning job {req.get('HyperParameterTuningJobName')} not found"
            )
        return _json_response({})

    def op_CreateDomain(self, req: dict) -> Response:
        """Register a SageMaker Studio domain."""
        if not req.get("DomainName"):
            return _error("ValidationException", "DomainName is required")
        return _json_response(self.executor.create_domain(req))

    def op_DescribeDomain(self, req: dict) -> Response:
        """Return a Studio domain record."""
        domain = self.executor.describe_domain(_require(req, "DomainId"))
        if domain is None:
            raise _NotFound(f"Domain {req.get('DomainId')} not found")
        return _json_response(domain)

    def op_UpdateDomain(self, req: dict) -> Response:
        """Update a Studio domain's settings."""
        result = self.executor.update_domain(req)
        if result is None:
            raise _NotFound(f"Domain {req.get('DomainId')} not found")
        return _json_response(result)

    def op_ListDomains(self, req: dict) -> Response:
        """Return a summary list of all Studio domains."""
        return _json_response({"Domains": self.executor.list_domains()})

    def op_DeleteDomain(self, req: dict) -> Response:
        """Delete a Studio domain."""
        if not self.executor.delete_domain(_require(req, "DomainId")):
            raise _NotFound(f"Domain {req.get('DomainId')} not found")
        return _json_response({})

    def op_CreateUserProfile(self, req: dict) -> Response:
        """Register a Studio user profile."""
        if not req.get("DomainId") or not req.get("UserProfileName"):
            return _error(
                "ValidationException", "DomainId and UserProfileName are required"
            )
        return _json_response(self.executor.create_user_profile(req))

    def op_DescribeUserProfile(self, req: dict) -> Response:
        """Return a Studio user-profile record."""
        profile = self.executor.describe_user_profile(
            _require(req, "DomainId"), _require(req, "UserProfileName")
        )
        if profile is None:
            raise _NotFound(f"UserProfile {req.get('UserProfileName')} not found")
        return _json_response(profile)

    def op_UpdateUserProfile(self, req: dict) -> Response:
        """Update a Studio user profile's settings."""
        result = self.executor.update_user_profile(req)
        if result is None:
            raise _NotFound(f"UserProfile {req.get('UserProfileName')} not found")
        return _json_response(result)

    def op_DeleteUserProfile(self, req: dict) -> Response:
        """Delete a Studio user profile."""
        if not self.executor.delete_user_profile(
            _require(req, "DomainId"), _require(req, "UserProfileName")
        ):
            raise _NotFound(f"UserProfile {req.get('UserProfileName')} not found")
        return _json_response({})

    def op_ListUserProfiles(self, req: dict) -> Response:
        """Return a summary list of user profiles (optionally by domain)."""
        return _json_response(
            {
                "UserProfiles": self.executor.list_user_profiles(
                    req.get("DomainIdEquals")
                )
            }
        )

    def op_CreateFeatureGroup(self, req: dict) -> Response:
        """Register a feature group (Feature Store control plane)."""
        for field in ("FeatureGroupName", "RecordIdentifierFeatureName", "EventTimeFeatureName"):
            if not req.get(field):
                return _error("ValidationException", f"{field} is required")
        return _json_response(
            {"FeatureGroupArn": self.executor.create_feature_group(req)}
        )

    def op_DescribeFeatureGroup(self, req: dict) -> Response:
        """Return a feature group's definition."""
        group = self.executor.describe_feature_group(
            _require(req, "FeatureGroupName")
        )
        if group is None:
            raise _NotFound(f"FeatureGroup {req.get('FeatureGroupName')} not found")
        return _json_response(group)

    def op_ListFeatureGroups(self, req: dict) -> Response:
        """Return a summary list of all feature groups."""
        return _json_response(
            {"FeatureGroupSummaries": self.executor.list_feature_groups()}
        )

    def op_DeleteFeatureGroup(self, req: dict) -> Response:
        """Delete a feature group."""
        self.executor.delete_feature_group(_require(req, "FeatureGroupName"))
        return _json_response({})

    def op_CreateMonitoringSchedule(self, req: dict) -> Response:
        """Register a Model Monitor schedule and run its analysis once."""
        if not req.get("MonitoringScheduleName"):
            return _error("ValidationException", "MonitoringScheduleName is required")
        return _json_response(
            {"MonitoringScheduleArn": self.executor.create_monitoring_schedule(req)}
        )

    def op_DescribeMonitoringSchedule(self, req: dict) -> Response:
        """Return a monitoring schedule (with its last execution status)."""
        schedule = self.executor.describe_monitoring_schedule(
            _require(req, "MonitoringScheduleName")
        )
        if schedule is None:
            raise _NotFound(
                f"MonitoringSchedule {req.get('MonitoringScheduleName')} not found"
            )
        return _json_response(schedule)

    def op_ListMonitoringSchedules(self, req: dict) -> Response:
        """Return a summary list of all monitoring schedules."""
        return _json_response(
            {"MonitoringScheduleSummaries": self.executor.list_monitoring_schedules()}
        )

    def op_DeleteMonitoringSchedule(self, req: dict) -> Response:
        """Delete a monitoring schedule."""
        self.executor.delete_monitoring_schedule(
            _require(req, "MonitoringScheduleName")
        )
        return _json_response({})

    async def feature_record(self, request: Request) -> Response:
        """featurestore-runtime Put/Get/DeleteRecord on /FeatureGroup/{name}."""
        from starlette.concurrency import run_in_threadpool

        name = request.path_params["name"]
        try:
            if request.method == "PUT":
                body = await request.body()
                record = (json.loads(body) if body else {}).get("Record", [])
                await run_in_threadpool(self.executor.put_record, name, record)
                return _rest_json({})
            if request.method == "DELETE":
                if not request.query_params.get("EventTime"):
                    return _rest_json(
                        {"Message": "EventTime is required"},
                        status=400,
                        error_type="ValidationException",
                    )
                rid = request.query_params.get("RecordIdentifierValueAsString", "")
                await run_in_threadpool(self.executor.delete_record, name, rid)
                return _rest_json({})
            # GET
            rid = request.query_params.get("RecordIdentifierValueAsString", "")
            features = request.query_params.getlist("FeatureName") or None
            record = await run_in_threadpool(
                self.executor.get_record, name, rid, features
            )
            return _rest_json({"Record": record} if record else {})
        except KeyError:
            return _rest_json(
                {"Message": f"feature group {name} not found"},
                status=404,
                error_type="ResourceNotFound",
            )
        except Exception as err:  # noqa: BLE001
            return _rest_json(
                {"Message": str(err)}, status=400, error_type="ValidationException"
            )

    async def batch_get_record(self, request: Request) -> Response:
        """featurestore-runtime BatchGetRecord on /BatchGetRecord."""
        from starlette.concurrency import run_in_threadpool

        body = await request.body()
        identifiers = (json.loads(body) if body else {}).get("Identifiers", [])
        result = await run_in_threadpool(
            self.executor.batch_get_record, identifiers
        )
        return _rest_json(result)

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
            # sagemaker-featurestore-runtime: REST record put/get/delete + batch
            Route(
                "/FeatureGroup/{name}",
                dispatcher.feature_record,
                methods=["PUT", "GET", "DELETE"],
            ),
            Route(
                "/BatchGetRecord",
                dispatcher.batch_get_record,
                methods=["POST"],
            ),
        ]
    )


app = create_app()
