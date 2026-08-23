"""SageMaker control-plane executor: runs training jobs locally in Docker.

Implements the boto3 ``sagemaker`` operations against oblako's own Docker
execution (``SageMakerService.run_training``) and the local object store
(S3Proxy), so unmodified boto3 / SageMaker code runs locally: a
``create_training_job`` pulls its input channels from S3, trains in a real
container per the ``/opt/ml`` contract, and writes ``model.tar.gz`` back to S3.
Jobs run in a background thread with the real status lifecycle
(InProgress -> Completed/Failed) so ``describe_training_job`` polling works.
"""

from __future__ import annotations

import datetime
import io
import os
import tarfile
import tempfile
import threading

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
        """Initialize the in-memory job store."""
        self._jobs: dict[str, dict] = {}
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
            channels: dict[str, str] = {}
            for channel in job["InputDataConfig"]:
                cname = channel["ChannelName"]
                uri = channel["DataSource"]["S3DataSource"]["S3Uri"]
                cdir = os.path.join(work, cname)
                os.makedirs(cdir, exist_ok=True)
                self._download_prefix(s3, uri, cdir)
                channels[cname] = cdir

            files = SageMakerService().run_training(
                image=image,
                channels=channels,
                hyperparameters=job.get("HyperParameters") or {},
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


def _tar_model(files: dict[str, bytes]) -> bytes:
    """Build a model.tar.gz from a ``{relative_path: bytes}`` map."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
