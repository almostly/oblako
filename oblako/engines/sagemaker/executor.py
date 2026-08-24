"""SageMaker control-plane + runtime executor: runs jobs/endpoints in local Docker.

Implements the boto3 ``sagemaker`` (and ``sagemaker-runtime``) operations against
oblako's own Docker execution and the local object store (S3Proxy), so unmodified
boto3 / SageMaker code runs locally:

- ``create_training_job`` pulls its input channels from S3, trains in a real
  container per the ``/opt/ml`` contract, and writes ``model.tar.gz`` back to S3.
- ``create_model`` / ``create_endpoint_config`` / ``create_endpoint`` start a
  long-running serving container (model loaded from S3), and
  ``invoke_endpoint`` proxies to its ``/invocations``.

Work runs in background threads with the real status lifecycle (InProgress ->
Completed/Failed, Creating -> InService/Failed) so ``describe_*`` polling works.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid

_ACCOUNT = "000000000000"
_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _s3_client():
    """boto3 S3 client for the local object store (S3Proxy), host-side."""
    import boto3
    from botocore.config import Config

    endpoint = (
        os.environ.get("AWS_ENDPOINT_URL_S3")
        or os.environ.get("AWS_ENDPOINT_URL")
        or "http://localhost:9000"
    )
    try:
        cfg = Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        )
    except TypeError:
        cfg = Config(s3={"addressing_style": "path"})
    kwargs = {"endpoint_url": endpoint, "config": cfg, "region_name": _REGION}
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        kwargs["aws_access_key_id"] = "oblako"
        kwargs["aws_secret_access_key"] = "oblako"
    return boto3.client("s3", **kwargs)


def _split_uri(uri: str) -> tuple[str, str]:
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    return bucket, key


class SageMakerExecutor:
    """Runs SageMaker training jobs in local Docker and tracks their state."""

    def __init__(self):
        """Initialize the in-memory stores."""
        self._jobs: dict[str, dict] = {}
        self._models: dict[str, dict] = {}
        self._endpoint_configs: dict[str, dict] = {}
        self._endpoints: dict[str, dict] = {}
        self._transform_jobs: dict[str, dict] = {}
        self._processing_jobs: dict[str, dict] = {}
        self._tuning_jobs: dict[str, dict] = {}
        self._tags: dict[str, list[dict]] = {}
        self._domains: dict[str, dict] = {}
        self._user_profiles: dict[tuple[str, str], dict] = {}
        self._feature_groups: dict[str, dict] = {}
        self._online: dict[str, dict[str, dict]] = {}
        self._monitoring_schedules: dict[str, dict] = {}
        self._stopping: set[str] = set()
        self._lock = threading.Lock()

    # -- training jobs -------------------------------------------------------
    def create_training_job(self, req: dict) -> str:
        """Register a training job and run it in the background; return its ARN."""
        name = req["TrainingJobName"]
        arn = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:training-job/{name}"
        job = {
            "TrainingJobName": name,
            "TrainingJobArn": arn,
            "TrainingJobStatus": "InProgress",
            "SecondaryStatus": "Starting",
            "AlgorithmSpecification": req.get("AlgorithmSpecification", {}),
            "HyperParameters": req.get("HyperParameters", {}),
            "InputDataConfig": req.get("InputDataConfig", []),
            "OutputDataConfig": req.get("OutputDataConfig", {}),
            "ResourceConfig": req.get("ResourceConfig", {}),
            "Environment": req.get("Environment", {}),
            "RoleArn": req.get("RoleArn"),
            "ModelArtifacts": {"S3ModelArtifacts": ""},
            "CreationTime": _now(),
            "TrainingStartTime": _now(),
        }
        with self._lock:
            self._jobs[name] = job
        threading.Thread(target=self._run, args=(name,), daemon=True).start()
        return arn

    def _run(self, name: str) -> None:
        """Download channels, train in a container, upload the model artifact."""
        job = self._jobs[name]
        work = tempfile.mkdtemp(prefix="sm-job-")
        try:
            from oblako.services import SageMakerService

            image = job["AlgorithmSpecification"]["TrainingImage"]
            s3 = _s3_client()
            channels = self._download_channels(s3, job["InputDataConfig"], work)

            instance_type = str(
                (job.get("ResourceConfig") or {}).get("InstanceType", "")
            )
            files = SageMakerService().run_training(
                image=image,
                channels=channels,
                hyperparameters=job.get("HyperParameters") or {},
                environment=job.get("Environment") or None,
                gpus=instance_type.endswith("local_gpu"),
                on_container=lambda c: self._track_container(job, c),
            )

            tar_bytes = _tar_model(files)
            out = job["OutputDataConfig"]["S3OutputPath"].rstrip("/")
            artifact_uri = f"{out}/{name}/output/model.tar.gz"
            bucket, key = _split_uri(artifact_uri)
            s3.put_object(Bucket=bucket, Key=key, Body=tar_bytes)

            with self._lock:
                job["TrainingJobStatus"] = "Completed"
                job["SecondaryStatus"] = "Completed"
                job["ModelArtifacts"] = {"S3ModelArtifacts": artifact_uri}
                job["TrainingEndTime"] = _now()
        except Exception as err:  # noqa: BLE001 - surface as a Failed/Stopped job
            with self._lock:
                stopped = name in self._stopping
                self._stopping.discard(name)
                job["TrainingJobStatus"] = "Stopped" if stopped else "Failed"
                job["SecondaryStatus"] = "Stopped" if stopped else "Failed"
                if not stopped:
                    job["FailureReason"] = str(err).strip()
                job["TrainingEndTime"] = _now()

    def _track_container(self, record: dict, container) -> None:
        """Record a job's running container id so ``Stop*`` can kill it."""
        with self._lock:
            record["_container"] = container.id

    def stop_training_job(self, name: str) -> bool:
        """Request a training job stop; kill its container if still running."""
        return self._stop_job(self._jobs, name, "TrainingJobStatus")

    def _stop_job(self, store: dict, name: str, status_key: str) -> bool:
        """Mark a job stopping and force-kill its container (shared by Stop*)."""
        with self._lock:
            job = store.get(name)
            if job is None:
                return False
            if job.get(status_key) not in ("InProgress", None):
                return True  # already terminal: Stop is idempotent
            job[status_key] = "Stopping"
            self._stopping.add(name)
            cid = job.get("_container")
        if cid:
            from oblako.services import SageMakerService

            with contextlib.suppress(Exception):
                SageMakerService().client.containers.get(cid).remove(force=True)
        return True

    def _download_channels(
        self, s3, input_data_config: list[dict], work: str
    ) -> dict[str, str]:
        """Download every input channel's S3 prefix into a per-channel local dir."""
        channels: dict[str, str] = {}
        for channel in input_data_config:
            cname = channel["ChannelName"]
            uri = channel["DataSource"]["S3DataSource"]["S3Uri"]
            cdir = os.path.join(work, cname)
            os.makedirs(cdir, exist_ok=True)
            self._download_prefix(s3, uri, cdir)
            channels[cname] = cdir
        return channels

    @staticmethod
    def _download_prefix(s3, uri: str, dest: str) -> None:
        """Download every object under an s3 prefix into ``dest`` (flattened)."""
        bucket, key = _split_uri(uri)
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=key).get("Contents", [])
        for obj in listed:
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            name = os.path.basename(obj["Key"]) or "data"
            with open(os.path.join(dest, name), "wb") as fh:
                fh.write(body)

    def describe_training_job(self, name: str) -> dict | None:
        """Return the public job record, or None if unknown."""
        with self._lock:
            job = self._jobs.get(name)
            return _public(job) if job else None

    def list_training_jobs(self) -> list[dict]:
        """Return a summary list of all training jobs."""
        with self._lock:
            return [
                {
                    "TrainingJobName": j["TrainingJobName"],
                    "TrainingJobArn": j["TrainingJobArn"],
                    "TrainingJobStatus": j["TrainingJobStatus"],
                    "CreationTime": j["CreationTime"],
                }
                for j in self._jobs.values()
            ]

    # -- models / endpoints --------------------------------------------------
    def create_model(self, req: dict) -> str:
        """Register a model (image + model data + env); return its ARN."""
        name = req["ModelName"]
        arn = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:model/{name}"
        with self._lock:
            self._models[name] = {
                "ModelName": name,
                "ModelArn": arn,
                "PrimaryContainer": req.get("PrimaryContainer", {}),
                "ExecutionRoleArn": req.get("ExecutionRoleArn"),
                "CreationTime": _now(),
            }
        return arn

    def describe_model(self, name: str) -> dict | None:
        """Return a model record, or None if unknown."""
        with self._lock:
            model = self._models.get(name)
            return _public(model) if model else None

    def list_models(self) -> list[dict]:
        """Return a summary list of all models."""
        with self._lock:
            return [
                {
                    "ModelName": m["ModelName"],
                    "ModelArn": m["ModelArn"],
                    "CreationTime": m["CreationTime"],
                }
                for m in self._models.values()
            ]

    def delete_model(self, name: str) -> None:
        """Remove a model registration (idempotent)."""
        with self._lock:
            self._models.pop(name, None)

    def create_endpoint_config(self, req: dict) -> str:
        """Register an endpoint config; return its ARN."""
        name = req["EndpointConfigName"]
        arn = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:endpoint-config/{name}"
        with self._lock:
            self._endpoint_configs[name] = {
                "EndpointConfigName": name,
                "EndpointConfigArn": arn,
                "ProductionVariants": req.get("ProductionVariants", []),
                "DataCaptureConfig": req.get("DataCaptureConfig", {}),
                "CreationTime": _now(),
            }
        return arn

    def describe_endpoint_config(self, name: str) -> dict | None:
        """Return an endpoint config record, or None if unknown."""
        with self._lock:
            config = self._endpoint_configs.get(name)
            return _public(config) if config else None

    def list_endpoint_configs(self) -> list[dict]:
        """Return a summary list of all endpoint configs."""
        with self._lock:
            return [
                {
                    "EndpointConfigName": c["EndpointConfigName"],
                    "EndpointConfigArn": c["EndpointConfigArn"],
                    "CreationTime": c["CreationTime"],
                }
                for c in self._endpoint_configs.values()
            ]

    def delete_endpoint_config(self, name: str) -> None:
        """Remove an endpoint config (idempotent)."""
        with self._lock:
            self._endpoint_configs.pop(name, None)

    def create_endpoint(self, req: dict) -> str:
        """Start a local serving container for the endpoint; return its ARN."""
        name = req["EndpointName"]
        config_name = req["EndpointConfigName"]
        arn = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:endpoint/{name}"
        with self._lock:
            self._endpoints[name] = {
                "EndpointName": name,
                "EndpointArn": arn,
                "EndpointConfigName": config_name,
                "EndpointStatus": "Creating",
                "CreationTime": _now(),
                "_container": None,
                "_port": None,
            }
        threading.Thread(
            target=self._start_endpoint, args=(name, config_name), daemon=True
        ).start()
        return arn

    def _start_serving_container(self, model: dict, container_name: str):
        """Start a serving container for a model, load its data, wait for /ping.

        Returns the running container and the host port its :8080 is published on.
        """
        from oblako.services import SageMakerService

        container_def = model["PrimaryContainer"]
        client = SageMakerService().client
        container = client.containers.create(
            container_def["Image"],
            environment=container_def.get("Environment", {}),
            ports={"8080/tcp": None},  # publish to a random host port
            name=container_name,
            detach=True,
        )
        if container_def.get("ModelDataUrl"):
            container.put_archive(
                "/", self._model_payload(container_def["ModelDataUrl"])
            )
        container.start()
        container.reload()
        host_port = int(container.ports["8080/tcp"][0]["HostPort"])
        self._await_ping(host_port)
        return container, host_port

    def _start_endpoint(self, name: str, config_name: str) -> None:
        """Run the serving container and mark the endpoint InService."""
        endpoint = self._endpoints[name]
        try:
            config = self._endpoint_configs[config_name]
            variants = config.get("ProductionVariants", [])
            model = self._models[variants[0]["ModelName"]]
            container, host_port = self._start_serving_container(
                model, f"sagemaker-local-endpoint-{name}"
            )
            # echo the config's variants (incl. any ServerlessConfig) in describe
            summaries = [
                {
                    "VariantName": v.get("VariantName"),
                    "CurrentInstanceCount": v.get("InitialInstanceCount"),
                    "CurrentServerlessConfig": v.get("ServerlessConfig"),
                }
                for v in variants
            ]
            with self._lock:
                endpoint.update(
                    EndpointStatus="InService",
                    ProductionVariants=summaries,
                    DataCaptureConfig=config.get("DataCaptureConfig", {}),
                    _container=container.id,
                    _port=host_port,
                    _variant=(variants[0].get("VariantName") if variants else None)
                    or "AllTraffic",
                )
        except Exception as err:  # noqa: BLE001 - surface as a Failed endpoint
            with self._lock:
                endpoint["EndpointStatus"] = "Failed"
                endpoint["FailureReason"] = str(err).strip()

    @staticmethod
    def _model_payload(model_url: str) -> bytes:
        """Download model.tar.gz from S3 and re-tar it rooted at /opt/ml/model."""
        s3 = _s3_client()
        bucket, key = _split_uri(model_url)
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        out = io.BytesIO()
        with (
            tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as src,
            tarfile.open(fileobj=out, mode="w") as dst,
        ):
            for member in src.getmembers():
                if not member.isfile():
                    continue
                data = src.extractfile(member).read()
                info = tarfile.TarInfo(f"opt/ml/model/{member.name}")
                info.size = len(data)
                dst.addfile(info, io.BytesIO(data))
        return out.getvalue()

    @staticmethod
    def _await_ping(host_port: int, timeout: float = 60.0) -> None:
        """Poll GET /ping on the serving container until it is healthy."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://localhost:{host_port}/ping", timeout=2
                ) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("serving container did not become healthy")

    def describe_endpoint(self, name: str) -> dict | None:
        """Return the public endpoint record (without internal fields)."""
        with self._lock:
            endpoint = self._endpoints.get(name)
            if endpoint is None:
                return None
            return _public(endpoint)

    def list_endpoints(self) -> list[dict]:
        """Return a summary list of all endpoints."""
        with self._lock:
            return [
                {
                    "EndpointName": e["EndpointName"],
                    "EndpointArn": e["EndpointArn"],
                    "EndpointStatus": e["EndpointStatus"],
                    "CreationTime": e["CreationTime"],
                }
                for e in self._endpoints.values()
            ]

    def delete_endpoint(self, name: str) -> None:
        """Stop and remove the endpoint's serving container."""
        with self._lock:
            endpoint = self._endpoints.pop(name, None)
        if endpoint and endpoint.get("_container"):
            from oblako.services import SageMakerService

            with contextlib.suppress(Exception):
                SageMakerService().client.containers.get(endpoint["_container"]).remove(
                    force=True
                )

    def invoke_endpoint(self, name: str, body: bytes, content_type: str) -> bytes:
        """Proxy an inference request to the endpoint's /invocations."""
        with self._lock:
            endpoint = self._endpoints.get(name)
            port = endpoint.get("_port") if endpoint else None
            status = endpoint.get("EndpointStatus") if endpoint else None
        if endpoint is None:
            raise KeyError(f"endpoint {name} not found")
        if status != "InService" or port is None:
            raise RuntimeError(f"endpoint {name} is not InService (status {status})")
        req = urllib.request.Request(
            f"http://localhost:{port}/invocations",
            data=body,
            method="POST",
            headers={"Content-Type": content_type or "application/octet-stream"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = resp.read()
            result_ct = resp.headers.get("Content-Type", content_type)
        self._capture_invocation(endpoint, body, content_type, result, result_ct)
        return result

    def _capture_invocation(
        self, endpoint: dict, request_body, request_ct, response_body, response_ct
    ) -> None:
        """Log an invocation to S3 in SageMaker's Data Capture JSONL format.

        Best-effort: capture must never break inference, so any failure (e.g. S3
        unreachable) is swallowed. One JSON Lines record per invocation is written
        under ``<DestinationS3Uri>/<endpoint>/<variant>/YYYY/MM/DD/HH/<uuid>.jsonl``,
        with the ``captureData`` envelope (endpointInput/endpointOutput) that a
        Model Monitor / the book's capture reader consumes unchanged.
        """
        cfg = endpoint.get("DataCaptureConfig") or {}
        if not cfg.get("EnableCapture") or not cfg.get("DestinationS3Uri"):
            return
        import random

        pct = cfg.get("InitialSamplingPercentage", 100)
        if pct < 100 and random.uniform(0, 100) > pct:  # noqa: S311 - not crypto
            return
        modes = {
            o.get("CaptureMode")
            for o in cfg.get(
                "CaptureOptions", [{"CaptureMode": "Input"}, {"CaptureMode": "Output"}]
            )
        }
        capture: dict = {}
        if "Input" in modes:
            capture["endpointInput"] = _capture_part(request_body, request_ct, "INPUT")
        if "Output" in modes:
            capture["endpointOutput"] = _capture_part(
                response_body, response_ct, "OUTPUT"
            )
        now = _now()
        record = {
            "captureData": capture,
            "eventMetadata": {
                "eventId": uuid.uuid4().hex,
                "inferenceTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "eventVersion": "0",
        }
        variant = endpoint.get("_variant") or "AllTraffic"
        base = cfg["DestinationS3Uri"].rstrip("/")
        key_prefix = f"{endpoint['EndpointName']}/{variant}/{now:%Y/%m/%d/%H}"
        bucket, prefix = _split_uri(f"{base}/{key_prefix}/{uuid.uuid4().hex}.jsonl")
        with contextlib.suppress(Exception):
            _s3_client().put_object(
                Bucket=bucket, Key=prefix, Body=(json.dumps(record) + "\n").encode()
            )

    # -- batch transform -----------------------------------------------------
    def create_transform_job(self, req: dict) -> str:
        """Register a batch transform job and run it in the background; return ARN."""
        name = req["TransformJobName"]
        arn = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:transform-job/{name}"
        with self._lock:
            self._transform_jobs[name] = {
                "TransformJobName": name,
                "TransformJobArn": arn,
                "TransformJobStatus": "InProgress",
                "ModelName": req["ModelName"],
                "TransformInput": req.get("TransformInput", {}),
                "TransformOutput": req.get("TransformOutput", {}),
                "CreationTime": _now(),
            }
        threading.Thread(target=self._run_transform, args=(name,), daemon=True).start()
        return arn

    def _run_transform(self, name: str) -> None:
        """Run each input object through the model's serving container to S3."""
        job = self._transform_jobs[name]
        container = None
        try:
            model = self._models[job["ModelName"]]
            container, port = self._start_serving_container(
                model, f"sagemaker-local-transform-{name}"
            )
            self._track_container(job, container)
            content_type = job["TransformInput"].get(
                "ContentType", "application/octet-stream"
            )
            in_uri = job["TransformInput"]["DataSource"]["S3DataSource"]["S3Uri"]
            out_base = job["TransformOutput"]["S3OutputPath"].rstrip("/")
            out_bucket, out_prefix = _split_uri(out_base)

            s3 = _s3_client()
            in_bucket, in_prefix = _split_uri(in_uri)
            for obj in s3.list_objects_v2(Bucket=in_bucket, Prefix=in_prefix).get(
                "Contents", []
            ):
                body = s3.get_object(Bucket=in_bucket, Key=obj["Key"])["Body"].read()
                request = urllib.request.Request(
                    f"http://localhost:{port}/invocations",
                    data=body,
                    method="POST",
                    headers={"Content-Type": content_type},
                )
                with urllib.request.urlopen(request, timeout=120) as resp:
                    result = resp.read()
                out_key = f"{out_prefix}/{os.path.basename(obj['Key'])}.out".lstrip("/")
                s3.put_object(Bucket=out_bucket, Key=out_key, Body=result)

            with self._lock:
                job["TransformJobStatus"] = "Completed"
                job["TransformEndTime"] = _now()
        except Exception as err:  # noqa: BLE001 - surface as a Failed/Stopped job
            with self._lock:
                stopped = name in self._stopping
                self._stopping.discard(name)
                job["TransformJobStatus"] = "Stopped" if stopped else "Failed"
                if not stopped:
                    job["FailureReason"] = str(err).strip()
                job["TransformEndTime"] = _now()
        finally:
            if container is not None:
                with contextlib.suppress(Exception):
                    container.remove(force=True)

    def stop_transform_job(self, name: str) -> bool:
        """Request a batch transform job stop; kill its container if running."""
        return self._stop_job(self._transform_jobs, name, "TransformJobStatus")

    def describe_transform_job(self, name: str) -> dict | None:
        """Return the public transform-job record, or None if unknown."""
        with self._lock:
            job = self._transform_jobs.get(name)
            return _public(job) if job else None

    def list_transform_jobs(self) -> list[dict]:
        """Return a summary list of all transform jobs."""
        with self._lock:
            return [
                {
                    "TransformJobName": j["TransformJobName"],
                    "TransformJobArn": j["TransformJobArn"],
                    "TransformJobStatus": j["TransformJobStatus"],
                    "CreationTime": j["CreationTime"],
                }
                for j in self._transform_jobs.values()
            ]

    # -- processing jobs (the ProcessingStep atom) ---------------------------
    def create_processing_job(self, req: dict) -> str:
        """Register a processing job and run it in the background; return its ARN."""
        name = req["ProcessingJobName"]
        arn = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:processing-job/{name}"
        with self._lock:
            self._processing_jobs[name] = {
                "ProcessingJobName": name,
                "ProcessingJobArn": arn,
                "ProcessingJobStatus": "InProgress",
                "AppSpecification": req.get("AppSpecification", {}),
                "ProcessingInputs": req.get("ProcessingInputs", []),
                "ProcessingOutputConfig": req.get("ProcessingOutputConfig", {}),
                "ProcessingResources": req.get("ProcessingResources", {}),
                "RoleArn": req.get("RoleArn"),
                "CreationTime": _now(),
            }
        threading.Thread(target=self._run_processing, args=(name,), daemon=True).start()
        return arn

    def _run_processing(self, name: str) -> None:
        """Copy inputs in, run the processing container, copy outputs to S3."""
        job = self._processing_jobs[name]
        container = None
        try:
            from oblako.services import SageMakerService

            app = job["AppSpecification"]
            client = SageMakerService().client
            container = client.containers.create(
                app["ImageUri"],
                entrypoint=app.get("ContainerEntrypoint"),
                command=app.get("ContainerArguments"),
                name=f"sagemaker-local-processing-{name}",
                detach=True,
            )
            self._track_container(job, container)
            s3 = _s3_client()
            for inp in job.get("ProcessingInputs", []):
                s3_input = inp["S3Input"]
                local = s3_input["LocalPath"].lstrip("/")
                container.put_archive(
                    "/", self._input_payload(s3, s3_input["S3Uri"], local)
                )
            container.start()
            result = container.wait(timeout=1800)
            code = result.get("StatusCode", 1)
            if code != 0:
                logs = container.logs().decode("utf-8", "replace")
                raise RuntimeError(
                    f"processing container exited {code}:\n{logs[-4000:]}"
                )
            for out in job.get("ProcessingOutputConfig", {}).get("Outputs", []):
                s3_output = out["S3Output"]
                self._upload_from_container(
                    container, s3_output["LocalPath"], s3_output["S3Uri"], s3
                )
            with self._lock:
                job["ProcessingJobStatus"] = "Completed"
                job["ProcessingEndTime"] = _now()
        except Exception as err:  # noqa: BLE001 - surface as a Failed/Stopped job
            with self._lock:
                stopped = name in self._stopping
                self._stopping.discard(name)
                job["ProcessingJobStatus"] = "Stopped" if stopped else "Failed"
                if not stopped:
                    job["FailureReason"] = str(err).strip()
                job["ProcessingEndTime"] = _now()
        finally:
            if container is not None:
                with contextlib.suppress(Exception):
                    container.remove(force=True)

    def stop_processing_job(self, name: str) -> bool:
        """Request a processing job stop; kill its container if running."""
        return self._stop_job(self._processing_jobs, name, "ProcessingJobStatus")

    @staticmethod
    def _input_payload(s3, uri: str, local_path: str) -> bytes:
        """Tar the objects under an s3 prefix, rooted at ``local_path`` (for put_archive)."""
        bucket, key = _split_uri(uri)
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w") as tar:
            for obj in s3.list_objects_v2(Bucket=bucket, Prefix=key).get(
                "Contents", []
            ):
                data = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                info = tarfile.TarInfo(f"{local_path}/{os.path.basename(obj['Key'])}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return out.getvalue()

    @staticmethod
    def _upload_from_container(container, local_path: str, uri: str, s3) -> None:
        """Copy a directory out of the container (docker cp) and upload it to S3."""
        bucket, prefix = _split_uri(uri.rstrip("/"))
        base = os.path.basename(local_path.rstrip("/"))
        bits, _ = container.get_archive(local_path)
        buf = io.BytesIO(b"".join(bits))
        buf.seek(0)
        with tarfile.open(fileobj=buf) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                rel = member.name
                if rel.startswith(f"{base}/"):
                    rel = rel[len(base) + 1 :]
                data = tar.extractfile(member).read()
                s3.put_object(
                    Bucket=bucket, Key=f"{prefix}/{rel}".lstrip("/"), Body=data
                )

    def describe_processing_job(self, name: str) -> dict | None:
        """Return the public processing-job record, or None if unknown."""
        with self._lock:
            job = self._processing_jobs.get(name)
            return _public(job) if job else None

    def list_processing_jobs(self) -> list[dict]:
        """Return a summary list of all processing jobs."""
        with self._lock:
            return [
                {
                    "ProcessingJobName": j["ProcessingJobName"],
                    "ProcessingJobArn": j["ProcessingJobArn"],
                    "ProcessingJobStatus": j["ProcessingJobStatus"],
                    "CreationTime": j["CreationTime"],
                }
                for j in self._processing_jobs.values()
            ]

    # -- automatic model tuning (HPO) ----------------------------------------
    def create_hyper_parameter_tuning_job(self, req: dict) -> str:
        """Register a tuning job and run the search in the background; return ARN."""
        name = req["HyperParameterTuningJobName"]
        arn = (
            f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}"
            f":hyper-parameter-tuning-job/{name}"
        )
        with self._lock:
            self._tuning_jobs[name] = {
                "HyperParameterTuningJobName": name,
                "HyperParameterTuningJobArn": arn,
                "HyperParameterTuningJobStatus": "InProgress",
                "HyperParameterTuningJobConfig": req.get(
                    "HyperParameterTuningJobConfig", {}
                ),
                "TrainingJobDefinition": req.get("TrainingJobDefinition", {}),
                "TrainingJobStatusCounters": {
                    "Completed": 0,
                    "InProgress": 0,
                    "RetryableError": 0,
                    "NonRetryableError": 0,
                    "Stopped": 0,
                },
                "ObjectiveStatusCounters": {
                    "Succeeded": 0,
                    "Pending": 0,
                    "Failed": 0,
                },
                "CreationTime": _now(),
            }
        threading.Thread(target=self._run_tuning, args=(name,), daemon=True).start()
        return arn

    def _run_tuning(self, name: str) -> None:
        """Run the search: each trial is a local training job, objective scraped.

        A searcher (Syne Tune's TPE when installed, else a built-in random
        sampler) proposes hyperparameters from ``ParameterRanges``; every
        proposal runs as a real training container, and the objective metric is
        read from the container's stdout with the ``MetricDefinitions`` regex.
        """
        job = self._tuning_jobs[name]
        work = tempfile.mkdtemp(prefix="sm-hpo-")
        try:
            from oblako.services import SageMakerService

            cfg = job["HyperParameterTuningJobConfig"]
            tdef = job["TrainingJobDefinition"]
            objective = cfg.get("HyperParameterTuningJobObjective", {})
            metric_name = objective.get("MetricName")
            do_min = objective.get("Type", "Maximize") == "Minimize"
            ranges = cfg.get("ParameterRanges", {})
            max_jobs = int(
                (cfg.get("ResourceLimits") or {}).get("MaxNumberOfTrainingJobs", 1)
            )
            strategy = cfg.get("Strategy", "Bayesian")

            algo = tdef.get("AlgorithmSpecification", {})
            image = algo.get("TrainingImage")
            regex = _metric_regex(algo.get("MetricDefinitions", []), metric_name)
            if not regex:
                raise RuntimeError(
                    f"no MetricDefinitions regex for objective metric '{metric_name}'"
                )
            static_hp = tdef.get("StaticHyperParameters", {})
            out_base = tdef["OutputDataConfig"]["S3OutputPath"].rstrip("/")

            s3 = _s3_client()
            channels = self._download_channels(
                s3, tdef.get("InputDataConfig", []), work
            )
            svc = SageMakerService()
            search = _make_search(ranges, do_min, strategy)

            best_value = None
            for i in range(max_jobs):
                with self._lock:
                    stop = name in self._stopping
                if stop:
                    break  # StopHyperParameterTuningJob: launch no more trials
                sampled = search.suggest()
                tuned = {k: _hp_str(v) for k, v in sampled.items()}
                trial_name = f"{name}-{i + 1:03d}"
                trial_arn = (
                    f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}"
                    f":training-job/{trial_name}"
                )
                trial = {
                    "TrainingJobName": trial_name,
                    "TrainingJobArn": trial_arn,
                    "TrainingJobStatus": "InProgress",
                    "SecondaryStatus": "Training",
                    "TuningJobArn": job["HyperParameterTuningJobArn"],
                    "HyperParameters": {**static_hp, **tuned},
                    "CreationTime": _now(),
                    "TrainingStartTime": _now(),
                }
                with self._lock:
                    self._jobs[trial_name] = trial
                try:
                    files, logs = svc.run_training(
                        image=image,
                        channels=channels,
                        hyperparameters={**static_hp, **tuned},
                        return_logs=True,
                        on_container=lambda c: self._track_container(job, c),
                    )
                    value = _scrape_metric(logs, regex)
                    if value is None:
                        raise RuntimeError(
                            f"objective '{metric_name}' not found in training logs"
                        )
                    artifact_uri = f"{out_base}/{trial_name}/output/model.tar.gz"
                    bucket, key = _split_uri(artifact_uri)
                    s3.put_object(Bucket=bucket, Key=key, Body=_tar_model(files))
                    final = {"MetricName": metric_name, "Value": value}
                    with self._lock:
                        trial.update(
                            TrainingJobStatus="Completed",
                            SecondaryStatus="Completed",
                            ModelArtifacts={"S3ModelArtifacts": artifact_uri},
                            TrainingEndTime=_now(),
                            FinalMetricDataList=[final],
                            TunedHyperParameters=tuned,
                        )
                        job["TrainingJobStatusCounters"]["Completed"] += 1
                        job["ObjectiveStatusCounters"]["Succeeded"] += 1
                    search.report(sampled, value)
                    better = best_value is None or (
                        value < best_value if do_min else value > best_value
                    )
                    if better:
                        best_value = value
                        with self._lock:
                            job["BestTrainingJob"] = {
                                "TrainingJobName": trial_name,
                                "TrainingJobArn": trial_arn,
                                "TrainingJobStatus": "Completed",
                                "TunedHyperParameters": tuned,
                                "FinalHyperParameterTuningJobObjectiveMetric": final,
                            }
                except Exception as err:  # noqa: BLE001 - record the trial as failed
                    with self._lock:
                        trial.update(
                            TrainingJobStatus="Failed",
                            SecondaryStatus="Failed",
                            FailureReason=str(err).strip(),
                            TrainingEndTime=_now(),
                        )
                        job["TrainingJobStatusCounters"]["NonRetryableError"] += 1
                        job["ObjectiveStatusCounters"]["Failed"] += 1

            with self._lock:
                stopped = name in self._stopping
                self._stopping.discard(name)
                job["HyperParameterTuningJobStatus"] = (
                    "Stopped" if stopped else "Completed"
                )
                job["HyperParameterTuningEndTime"] = _now()
        except Exception as err:  # noqa: BLE001 - surface as a Failed tuning job
            with self._lock:
                job["HyperParameterTuningJobStatus"] = "Failed"
                job["FailureReason"] = str(err).strip()
                job["HyperParameterTuningEndTime"] = _now()

    def stop_hyper_parameter_tuning_job(self, name: str) -> bool:
        """Request a tuning job stop; kill the in-flight trial and launch no more."""
        return self._stop_job(
            self._tuning_jobs, name, "HyperParameterTuningJobStatus"
        )

    def describe_hyper_parameter_tuning_job(self, name: str) -> dict | None:
        """Return the public tuning-job record, or None if unknown."""
        with self._lock:
            job = self._tuning_jobs.get(name)
            return _public(job) if job else None

    def list_hyper_parameter_tuning_jobs(self) -> list[dict]:
        """Return a summary list of all tuning jobs."""
        with self._lock:
            return [
                {
                    "HyperParameterTuningJobName": j["HyperParameterTuningJobName"],
                    "HyperParameterTuningJobArn": j["HyperParameterTuningJobArn"],
                    "HyperParameterTuningJobStatus": j[
                        "HyperParameterTuningJobStatus"
                    ],
                    "CreationTime": j["CreationTime"],
                }
                for j in self._tuning_jobs.values()
            ]

    # -- tags ----------------------------------------------------------------
    def add_tags(self, resource_arn: str, tags: list[dict]) -> list[dict]:
        """Attach tags to a resource (new keys overwrite existing), return all."""
        with self._lock:
            existing = {t["Key"]: t["Value"] for t in self._tags.get(resource_arn, [])}
            for tag in tags or []:
                existing[tag["Key"]] = tag["Value"]
            merged = [{"Key": k, "Value": v} for k, v in existing.items()]
            self._tags[resource_arn] = merged
            return list(merged)

    def delete_tags(self, resource_arn: str, tag_keys: list[str]) -> None:
        """Remove the given tag keys from a resource."""
        with self._lock:
            keep = [
                t
                for t in self._tags.get(resource_arn, [])
                if t["Key"] not in set(tag_keys or [])
            ]
            self._tags[resource_arn] = keep

    def list_tags(self, resource_arn: str) -> list[dict]:
        """Return the tags attached to a resource."""
        with self._lock:
            return list(self._tags.get(resource_arn, []))

    # -- SageMaker Studio: domains + user profiles ---------------------------
    def create_domain(self, req: dict) -> dict:
        """Register a Studio domain; return {DomainArn, DomainId, Url}."""
        domain_id = f"d-{uuid.uuid4().hex[:12]}"
        arn = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:domain/{domain_id}"
        url = f"https://{domain_id}.studio.{_REGION}.sagemaker.aws"
        now = _now()
        with self._lock:
            self._domains[domain_id] = {
                "DomainId": domain_id,
                "DomainArn": arn,
                "DomainName": req.get("DomainName"),
                "Url": url,
                "Status": "InService",
                "AuthMode": req.get("AuthMode"),
                "DefaultUserSettings": req.get("DefaultUserSettings", {}),
                "SubnetIds": req.get("SubnetIds"),
                "VpcId": req.get("VpcId"),
                "AppNetworkAccessType": req.get(
                    "AppNetworkAccessType", "PublicInternetOnly"
                ),
                "CreationTime": now,
                "LastModifiedTime": now,
            }
        return {"DomainArn": arn, "DomainId": domain_id, "Url": url}

    def describe_domain(self, domain_id: str) -> dict | None:
        """Return a Studio domain record, or None if unknown."""
        with self._lock:
            domain = self._domains.get(domain_id)
            return _public(domain) if domain else None

    def update_domain(self, req: dict) -> dict | None:
        """Update a domain's mutable settings; return {DomainArn} or None."""
        with self._lock:
            domain = self._domains.get(req.get("DomainId"))
            if domain is None:
                return None
            for key in (
                "DefaultUserSettings",
                "SubnetIds",
                "AppNetworkAccessType",
                "DefaultSpaceSettings",
            ):
                if req.get(key) is not None:
                    domain[key] = req[key]
            if req.get("DomainSettingsForUpdate") is not None:
                domain["DomainSettings"] = req["DomainSettingsForUpdate"]
            domain["LastModifiedTime"] = _now()
            return {"DomainArn": domain["DomainArn"]}

    def list_domains(self) -> list[dict]:
        """Return a summary list of all Studio domains."""
        with self._lock:
            return [
                {
                    "DomainId": d["DomainId"],
                    "DomainArn": d["DomainArn"],
                    "DomainName": d["DomainName"],
                    "Status": d["Status"],
                    "Url": d["Url"],
                    "CreationTime": d["CreationTime"],
                    "LastModifiedTime": d["LastModifiedTime"],
                }
                for d in self._domains.values()
            ]

    def delete_domain(self, domain_id: str) -> bool:
        """Remove a Studio domain; False if it did not exist."""
        with self._lock:
            return self._domains.pop(domain_id, None) is not None

    def create_user_profile(self, req: dict) -> dict:
        """Register a Studio user profile; return {UserProfileArn}."""
        domain_id = req["DomainId"]
        name = req["UserProfileName"]
        arn = (
            f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}"
            f":user-profile/{domain_id}/{name}"
        )
        now = _now()
        with self._lock:
            self._user_profiles[domain_id, name] = {
                "DomainId": domain_id,
                "UserProfileName": name,
                "UserProfileArn": arn,
                "Status": "InService",
                "UserSettings": req.get("UserSettings", {}),
                "SingleSignOnUserIdentifier": req.get("SingleSignOnUserIdentifier"),
                "SingleSignOnUserValue": req.get("SingleSignOnUserValue"),
                "CreationTime": now,
                "LastModifiedTime": now,
            }
        return {"UserProfileArn": arn}

    def describe_user_profile(self, domain_id: str, name: str) -> dict | None:
        """Return a Studio user-profile record, or None if unknown."""
        with self._lock:
            profile = self._user_profiles.get((domain_id, name))
            return _public(profile) if profile else None

    def update_user_profile(self, req: dict) -> dict | None:
        """Update a user profile's settings; return {UserProfileArn} or None."""
        with self._lock:
            profile = self._user_profiles.get(
                (req.get("DomainId"), req.get("UserProfileName"))
            )
            if profile is None:
                return None
            if req.get("UserSettings") is not None:
                profile["UserSettings"] = req["UserSettings"]
            profile["LastModifiedTime"] = _now()
            return {"UserProfileArn": profile["UserProfileArn"]}

    def delete_user_profile(self, domain_id: str, name: str) -> bool:
        """Remove a Studio user profile; False if it did not exist."""
        with self._lock:
            return self._user_profiles.pop((domain_id, name), None) is not None

    def list_user_profiles(self, domain_id: str | None = None) -> list[dict]:
        """Return a summary list of user profiles, optionally filtered by domain."""
        with self._lock:
            return [
                {
                    "DomainId": p["DomainId"],
                    "UserProfileName": p["UserProfileName"],
                    "Status": p["Status"],
                    "CreationTime": p["CreationTime"],
                    "LastModifiedTime": p["LastModifiedTime"],
                }
                for p in self._user_profiles.values()
                if domain_id is None or p["DomainId"] == domain_id
            ]

    # -- Feature Store: control plane ----------------------------------------
    def create_feature_group(self, req: dict) -> str:
        """Register a feature group (online KV + offline S3 Parquet); return ARN."""
        name = req["FeatureGroupName"]
        arn = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:feature-group/{name}"
        with self._lock:
            self._feature_groups[name] = {
                "FeatureGroupName": name,
                "FeatureGroupArn": arn,
                "RecordIdentifierFeatureName": req["RecordIdentifierFeatureName"],
                "EventTimeFeatureName": req["EventTimeFeatureName"],
                "FeatureDefinitions": req.get("FeatureDefinitions", []),
                "OnlineStoreConfig": req.get("OnlineStoreConfig", {}),
                "OfflineStoreConfig": req.get("OfflineStoreConfig", {}),
                "RoleArn": req.get("RoleArn"),
                "FeatureGroupStatus": "Created",
                "CreationTime": _now(),
            }
            self._online.setdefault(name, {})
        return arn

    def describe_feature_group(self, name: str) -> dict | None:
        """Return a feature group's definition, or None if unknown."""
        with self._lock:
            group = self._feature_groups.get(name)
            return _public(group) if group else None

    def list_feature_groups(self) -> list[dict]:
        """Return a summary list of all feature groups."""
        with self._lock:
            return [
                {
                    "FeatureGroupName": g["FeatureGroupName"],
                    "FeatureGroupArn": g["FeatureGroupArn"],
                    "FeatureGroupStatus": g["FeatureGroupStatus"],
                    "CreationTime": g["CreationTime"],
                }
                for g in self._feature_groups.values()
            ]

    def delete_feature_group(self, name: str) -> None:
        """Remove a feature group and its online records (idempotent)."""
        with self._lock:
            self._feature_groups.pop(name, None)
            self._online.pop(name, None)

    # -- Feature Store: data plane (featurestore-runtime) --------------------
    def put_record(self, name: str, record: list[dict]) -> None:
        """Write a record to the online store and append it to the offline store."""
        with self._lock:
            meta = self._feature_groups.get(name)
            if meta is None:
                raise KeyError(name)
            online_enabled = bool(meta["OnlineStoreConfig"].get("EnableOnlineStore"))
            offline_uri = (
                (meta["OfflineStoreConfig"].get("S3StorageConfig") or {}).get("S3Uri")
            )
        values = {f["FeatureName"]: f["ValueAsString"] for f in record}
        if meta["RecordIdentifierFeatureName"] not in values:
            raise ValueError(
                f"record is missing identifier '{meta['RecordIdentifierFeatureName']}'"
            )
        rid = values[meta["RecordIdentifierFeatureName"]]
        if online_enabled:
            with self._lock:
                self._online.setdefault(name, {})[rid] = values
        if offline_uri:
            _offline_append(name, meta, values, offline_uri)

    def get_record(
        self, name: str, record_id: str, feature_names: list[str] | None
    ) -> list[dict]:
        """Read one record from the online store; [] if the record is absent."""
        with self._lock:
            if name not in self._feature_groups:
                raise KeyError(name)
            values = self._online.get(name, {}).get(record_id)
        if not values:
            return []
        keep = feature_names or list(values)
        return [
            {"FeatureName": k, "ValueAsString": str(v)}
            for k, v in values.items()
            if k in keep
        ]

    def delete_record(self, name: str, record_id: str) -> None:
        """Remove a record from the online store."""
        with self._lock:
            if name not in self._feature_groups:
                raise KeyError(name)
            self._online.get(name, {}).pop(record_id, None)

    def batch_get_record(self, identifiers: list[dict]) -> dict:
        """Serve BatchGetRecord across feature groups and identifiers."""
        records, errors = [], []
        for ident in identifiers or []:
            name = ident["FeatureGroupName"]
            features = ident.get("FeatureNames")
            for rid in ident.get("RecordIdentifiersValueAsString", []):
                try:
                    rec = self.get_record(name, rid, features)
                except KeyError:
                    errors.append(
                        {
                            "FeatureGroupName": name,
                            "RecordIdentifierValueAsString": rid,
                            "ErrorCode": "ResourceNotFound",
                            "ErrorMessage": f"feature group {name} not found",
                        }
                    )
                    continue
                if rec:
                    records.append(
                        {
                            "FeatureGroupName": name,
                            "RecordIdentifierValueAsString": rid,
                            "Record": rec,
                        }
                    )
        return {"Records": records, "Errors": errors, "UnprocessedIdentifiers": []}

    # -- Model Monitor: monitoring schedules ---------------------------------
    def create_monitoring_schedule(self, req: dict) -> str:
        """Register a monitoring schedule and run its analysis once; return ARN.

        Model Monitor's analysis is a processing job that reads an endpoint's
        captured data (and an optional baseline) and writes a violations report.
        A real schedule runs it on a cron; oblako runs it once immediately (the
        simulated-topology equivalent) and records it as the last execution.
        """
        name = req["MonitoringScheduleName"]
        arn = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:monitoring-schedule/{name}"
        cfg = req.get("MonitoringScheduleConfig", {})
        now = _now()
        with self._lock:
            self._monitoring_schedules[name] = {
                "MonitoringScheduleName": name,
                "MonitoringScheduleArn": arn,
                "MonitoringScheduleStatus": "Scheduled",
                "MonitoringType": cfg.get("MonitoringType", "DataQuality"),
                "MonitoringScheduleConfig": cfg,
                "CreationTime": now,
                "LastModifiedTime": now,
            }
        summary = self._run_monitoring_once(name, cfg)
        if summary is not None:
            with self._lock:
                self._monitoring_schedules[name]["LastMonitoringExecutionSummary"] = (
                    summary
                )
        return arn

    def _run_monitoring_once(self, name: str, cfg: dict) -> dict | None:
        """Translate a MonitoringJobDefinition to a processing job and run it."""
        jobdef = cfg.get("MonitoringJobDefinition")
        if not jobdef:
            return None
        app = jobdef.get("MonitoringAppSpecification", {})
        image = app.get("ImageUri")
        if not image:
            return None
        inputs: list[dict] = []
        for mi in jobdef.get("MonitoringInputs", []):
            endpoint_input = mi.get("EndpointInput")
            if endpoint_input:
                uri = self._capture_uri_for(endpoint_input.get("EndpointName"))
                if uri:
                    inputs.append(
                        {
                            "InputName": "endpoint",
                            "S3Input": {
                                "S3Uri": uri,
                                "LocalPath": endpoint_input.get(
                                    "LocalPath", "/opt/ml/processing/input/endpoint"
                                ),
                                "S3DataType": "S3Prefix",
                            },
                        }
                    )
            elif mi.get("S3Input"):  # oblako convenience: a direct S3 input
                inputs.append(
                    {"InputName": mi.get("InputName", "input"), "S3Input": mi["S3Input"]}
                )
        base = jobdef.get("BaselineConfig", {})
        for key, local in (
            ("ConstraintsResource", "constraints"),
            ("StatisticsResource", "statistics"),
        ):
            uri = (base.get(key) or {}).get("S3Uri")
            if uri:
                inputs.append(
                    {
                        "InputName": local,
                        "S3Input": {
                            "S3Uri": uri,
                            "LocalPath": f"/opt/ml/processing/baseline/{local}",
                            "S3DataType": "S3Prefix",
                        },
                    }
                )
        outputs = [
            {
                "OutputName": mo.get("S3Output", {}).get("OutputName", "result"),
                "S3Output": {
                    "S3Uri": mo["S3Output"]["S3Uri"],
                    "LocalPath": mo["S3Output"].get(
                        "LocalPath", "/opt/ml/processing/output"
                    ),
                    "S3UploadMode": mo["S3Output"].get("S3UploadMode", "EndOfJob"),
                },
            }
            for mo in jobdef.get("MonitoringOutputConfig", {}).get("MonitoringOutputs", [])
            if mo.get("S3Output", {}).get("S3Uri")
        ]
        proc_name = f"{name}-{uuid.uuid4().hex[:8]}"
        self.create_processing_job(
            {
                "ProcessingJobName": proc_name,
                "AppSpecification": {
                    "ImageUri": image,
                    "ContainerEntrypoint": app.get("ContainerEntrypoint"),
                    "ContainerArguments": app.get("ContainerArguments"),
                },
                "ProcessingInputs": inputs,
                "ProcessingOutputConfig": {"Outputs": outputs},
                "ProcessingResources": jobdef.get("MonitoringResources", {}),
                "RoleArn": jobdef.get("RoleArn"),
            }
        )
        return {
            "MonitoringScheduleName": name,
            "ScheduledTime": _now(),
            "CreationTime": _now(),
            "MonitoringExecutionStatus": "InProgress",
            "ProcessingJobArn": (
                f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:processing-job/{proc_name}"
            ),
            "_processing_job": proc_name,
        }

    def _capture_uri_for(self, endpoint_name: str | None) -> str | None:
        """Return the S3 prefix an endpoint captures its invocations to."""
        with self._lock:
            endpoint = self._endpoints.get(endpoint_name)
            cfg = (endpoint or {}).get("DataCaptureConfig") or {}
        dest = cfg.get("DestinationS3Uri")
        return f"{dest.rstrip('/')}/{endpoint_name}" if dest else None

    def describe_monitoring_schedule(self, name: str) -> dict | None:
        """Return a schedule, refreshing its last execution's status."""
        with self._lock:
            schedule = self._monitoring_schedules.get(name)
            if schedule is None:
                return None
            summary = schedule.get("LastMonitoringExecutionSummary")
            proc = (summary or {}).get("_processing_job")
        if proc:
            job = self.describe_processing_job(proc)
            status = (job or {}).get("ProcessingJobStatus", "InProgress")
            mapped = {
                "Completed": "Completed",
                "Failed": "Failed",
                "Stopped": "Stopped",
                "InProgress": "InProgress",
            }.get(status, "InProgress")
            with self._lock:
                schedule["LastMonitoringExecutionSummary"][
                    "MonitoringExecutionStatus"
                ] = mapped
        with self._lock:
            return _public_deep(self._monitoring_schedules[name])

    def list_monitoring_schedules(self) -> list[dict]:
        """Return a summary list of all monitoring schedules."""
        with self._lock:
            return [
                {
                    "MonitoringScheduleName": s["MonitoringScheduleName"],
                    "MonitoringScheduleArn": s["MonitoringScheduleArn"],
                    "MonitoringScheduleStatus": s["MonitoringScheduleStatus"],
                    "MonitoringType": s["MonitoringType"],
                    "CreationTime": s["CreationTime"],
                    "LastModifiedTime": s["LastModifiedTime"],
                }
                for s in self._monitoring_schedules.values()
            ]

    def delete_monitoring_schedule(self, name: str) -> None:
        """Remove a monitoring schedule (idempotent)."""
        with self._lock:
            self._monitoring_schedules.pop(name, None)


def _offline_append(name: str, meta: dict, values: dict, offline_uri: str) -> None:
    """Append one record to a feature group's offline store as S3 Parquet.

    Writes one Parquet object per record under ``<S3Uri>/<group>/data/``, the
    physical layout of a real offline store, so the data is immediately
    queryable (DuckDB/Spectrum/awswrangler) with columns typed per the group's
    FeatureDefinitions.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    types = {
        d["FeatureName"]: d.get("FeatureType", "String")
        for d in meta.get("FeatureDefinitions", [])
    }
    columns = {
        k: pa.array([_cast_feature(v, types.get(k, "String"))], _arrow_type(types.get(k, "String")))
        for k, v in values.items()
    }
    buf = io.BytesIO()
    pq.write_table(pa.table(columns), buf)
    bucket, prefix = _split_uri(offline_uri.rstrip("/"))
    key = f"{prefix}/{name}/data/{uuid.uuid4().hex}.parquet".lstrip("/")
    _s3_client().put_object(Bucket=bucket, Key=key, Body=buf.getvalue())


def _cast_feature(value: str, feature_type: str):
    """Cast a ValueAsString to its FeatureDefinition type (fallback to string)."""
    try:
        if feature_type == "Integral":
            return int(value)
        if feature_type == "Fractional":
            return float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(value)


def _arrow_type(feature_type: str):
    """Map a SageMaker FeatureType to a pyarrow type."""
    import pyarrow as pa

    if feature_type == "Integral":
        return pa.int64()
    if feature_type == "Fractional":
        return pa.float64()
    return pa.string()


def _public(record: dict) -> dict:
    """Copy a record without internal (underscore-prefixed) bookkeeping fields."""
    return {k: v for k, v in record.items() if not k.startswith("_")}


def _public_deep(record: dict) -> dict:
    """Like ``_public`` but recurse into nested dicts (strip internal fields)."""
    out = {}
    for key, value in record.items():
        if key.startswith("_"):
            continue
        out[key] = _public_deep(value) if isinstance(value, dict) else value
    return out


def _capture_part(data: bytes, content_type: str | None, mode: str) -> dict:
    """Build a Data Capture endpointInput/endpointOutput part.

    Text payloads (CSV/JSON/plain) are stored as-is with a CSV/JSON encoding; any
    other content type is base64-encoded with a BASE64 encoding, matching how a
    real endpoint captures binary bodies (and how the reader decodes them).
    """
    import base64

    ct = content_type or "application/octet-stream"
    raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode()
    if ct.startswith("text/csv"):
        encoding, payload = "CSV", raw.decode("utf-8", "replace")
    elif ct.startswith("application/json") or ct.startswith("text/"):
        encoding, payload = "JSON", raw.decode("utf-8", "replace")
    else:
        encoding, payload = "BASE64", base64.b64encode(raw).decode("ascii")
    return {"observedContentType": ct, "mode": mode, "data": payload, "encoding": encoding}


def _hp_str(value) -> str:
    """Stringify a sampled hyperparameter the way SageMaker reports tuned values."""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _metric_regex(metric_definitions: list[dict], metric_name: str) -> str | None:
    """Find the regex whose metric Name matches the tuning objective."""
    for md in metric_definitions or []:
        if md.get("Name") == metric_name:
            return md.get("Regex")
    return None


def _scrape_metric(logs: str, regex: str) -> float | None:
    """Return the last value the objective regex matches in the container logs."""
    import re

    matches = re.findall(regex, logs)
    if not matches:
        return None
    last = matches[-1]
    if isinstance(last, tuple):
        last = last[0]
    try:
        return float(last)
    except (TypeError, ValueError):
        return None


def _make_search(ranges: dict, do_minimize: bool, strategy: str):
    """Pick a searcher: Syne Tune TPE when available, else built-in random.

    ``Strategy="Random"`` forces the random sampler; anything else prefers Syne
    Tune's Bayesian TPE and falls back to random if Syne Tune isn't installed.
    """
    if str(strategy).lower().startswith("random"):
        return _RandomSearch(ranges)
    try:
        return _SyneTuneSearch(ranges, do_minimize)
    except Exception:  # noqa: BLE001 - syne-tune not installed / unusable
        return _RandomSearch(ranges)


class _RandomSearch:
    """Dependency-free uniform random search over the AMT parameter ranges."""

    def __init__(self, ranges: dict):
        """Store the raw ``ParameterRanges`` to sample from."""
        self._ranges = ranges

    def suggest(self) -> dict:
        """Sample one configuration uniformly from each parameter's range."""
        import math
        import random

        cfg: dict = {}
        for r in self._ranges.get("ContinuousParameterRanges", []):
            lo, hi = float(r["MinValue"]), float(r["MaxValue"])
            if str(r.get("ScalingType")) == "Logarithmic" and lo > 0:
                cfg[r["Name"]] = math.exp(
                    random.uniform(math.log(lo), math.log(hi))
                )
            else:
                cfg[r["Name"]] = random.uniform(lo, hi)
        for r in self._ranges.get("IntegerParameterRanges", []):
            cfg[r["Name"]] = random.randint(int(r["MinValue"]), int(r["MaxValue"]))
        for r in self._ranges.get("CategoricalParameterRanges", []):
            cfg[r["Name"]] = random.choice(list(r["Values"]))
        return cfg

    def report(self, config: dict, value: float) -> None:
        """No-op: random search ignores observed objectives."""


class _SyneTuneSearch:
    """Bayesian search backed by Syne Tune's TPE scheduler (ask/tell)."""

    def __init__(self, ranges: dict, do_minimize: bool):
        """Build the Syne Tune config space + TPE scheduler from AMT ranges."""
        from syne_tune.config_space import choice, loguniform, randint, uniform
        from syne_tune.optimizer.baselines import TPE

        space: dict = {}
        for r in ranges.get("ContinuousParameterRanges", []):
            lo, hi = float(r["MinValue"]), float(r["MaxValue"])
            log = str(r.get("ScalingType")) == "Logarithmic" and lo > 0
            space[r["Name"]] = loguniform(lo, hi) if log else uniform(lo, hi)
        for r in ranges.get("IntegerParameterRanges", []):
            space[r["Name"]] = randint(int(r["MinValue"]), int(r["MaxValue"]))
        for r in ranges.get("CategoricalParameterRanges", []):
            space[r["Name"]] = choice(list(r["Values"]))
        if not space:
            raise ValueError("no tunable parameters for Syne Tune")
        self._metric = "objective"
        self._scheduler = TPE(
            config_space=space,
            metric=self._metric,
            do_minimize=do_minimize,
            random_seed=0,
        )
        self._trial_id = 0

    def suggest(self) -> dict:
        """Ask the scheduler for the next configuration to evaluate."""
        suggestion = self._scheduler.suggest()
        return dict(suggestion.config)

    def report(self, config: dict, value: float) -> None:
        """Tell the scheduler the objective observed for a configuration."""
        from syne_tune.backend.trial_status import Trial

        trial = Trial(
            trial_id=self._trial_id,
            config=config,
            creation_time=datetime.datetime.now(),  # noqa: DTZ005 - local only
        )
        self._scheduler.on_trial_complete(trial, {self._metric: value})
        self._trial_id += 1


def _tar_model(files: dict[str, bytes]) -> bytes:
    """Build a model.tar.gz from a ``{relative_path: bytes}`` map."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
