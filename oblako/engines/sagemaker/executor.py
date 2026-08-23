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
import os
import tarfile
import tempfile
import threading
import time
import urllib.request

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
        except Exception as err:  # noqa: BLE001 - surface as a Failed job
            with self._lock:
                job["TrainingJobStatus"] = "Failed"
                job["SecondaryStatus"] = "Failed"
                job["FailureReason"] = str(err).strip()
                job["TrainingEndTime"] = _now()

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
            return dict(job) if job else None

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

    def create_endpoint_config(self, req: dict) -> str:
        """Register an endpoint config; return its ARN."""
        name = req["EndpointConfigName"]
        arn = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:endpoint-config/{name}"
        with self._lock:
            self._endpoint_configs[name] = {
                "EndpointConfigName": name,
                "EndpointConfigArn": arn,
                "ProductionVariants": req.get("ProductionVariants", []),
                "CreationTime": _now(),
            }
        return arn

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
            model = self._models[config["ProductionVariants"][0]["ModelName"]]
            container, host_port = self._start_serving_container(
                model, f"sagemaker-local-endpoint-{name}"
            )
            with self._lock:
                endpoint.update(
                    EndpointStatus="InService", _container=container.id, _port=host_port
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
            return {k: v for k, v in endpoint.items() if not k.startswith("_")}

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
            return resp.read()

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
        except Exception as err:  # noqa: BLE001 - surface as a Failed job
            with self._lock:
                job["TransformJobStatus"] = "Failed"
                job["FailureReason"] = str(err).strip()
                job["TransformEndTime"] = _now()
        finally:
            if container is not None:
                with contextlib.suppress(Exception):
                    container.remove(force=True)

    def describe_transform_job(self, name: str) -> dict | None:
        """Return the public transform-job record, or None if unknown."""
        with self._lock:
            job = self._transform_jobs.get(name)
            return dict(job) if job else None

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
        except Exception as err:  # noqa: BLE001 - surface as a Failed job
            with self._lock:
                job["ProcessingJobStatus"] = "Failed"
                job["FailureReason"] = str(err).strip()
                job["ProcessingEndTime"] = _now()
        finally:
            if container is not None:
                with contextlib.suppress(Exception):
                    container.remove(force=True)

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
            return dict(job) if job else None

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
                job["HyperParameterTuningJobStatus"] = "Completed"
                job["HyperParameterTuningEndTime"] = _now()
        except Exception as err:  # noqa: BLE001 - surface as a Failed tuning job
            with self._lock:
                job["HyperParameterTuningJobStatus"] = "Failed"
                job["FailureReason"] = str(err).strip()
                job["HyperParameterTuningEndTime"] = _now()

    def describe_hyper_parameter_tuning_job(self, name: str) -> dict | None:
        """Return the public tuning-job record, or None if unknown."""
        with self._lock:
            job = self._tuning_jobs.get(name)
            return dict(job) if job else None

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
