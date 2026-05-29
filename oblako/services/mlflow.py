"""Local MLflow App: managed container running the MLflow tracking server.

On AWS, MLflow is a SageMaker feature (``sagemaker:CreateMlflowTrackingServer``)
— here it lives under the same namespace: ``oblako.sagemaker.mlflow``.

Experience-faithful: point ``mlflow`` at ``http://localhost:5050`` and the rest
is real MLflow. Artifacts land in S3Proxy via MLflow's proxied artifact storage
(clients upload via the server; only the server hits S3); the backend store is
SQLite on a named volume. ``sagemaker-mlflow`` is baked in for SigV4-signed
clients targeting SageMaker-managed MLflow.

The image is built from ``oblako/mlflow/Dockerfile`` on first start; if the
``slim`` (docker-slim / SlimToolkit) CLI is on PATH, the built image is then
minified in place.
"""

from __future__ import annotations

from oblako import ports
import shutil
import subprocess
from pathlib import Path

import boto3
import httpx
from botocore.config import Config as BotoConfig

from oblako import config
from .base import Service, PortMapping

IMAGE_TAG = "oblako-mlflow:latest"
ARTIFACT_BUCKET = "oblako-mlflow"
_DOCKERFILE_DIR = Path(__file__).resolve().parent.parent / "mlflow"


def _ensure_bucket(s3_endpoint: str) -> None:
    """Create the artifact bucket on S3Proxy if it does not exist (best-effort)."""
    try:
        s3 = boto3.client(
            "s3", endpoint_url=s3_endpoint, region_name=config.region(),
            aws_access_key_id="test", aws_secret_access_key="test",
            config=BotoConfig(signature_version="s3v4",
                              s3={"addressing_style": "path"},
                              request_checksum_calculation="when_required",
                              response_checksum_validation="when_required"),
        )
        if ARTIFACT_BUCKET not in {b["Name"] for b in s3.list_buckets().get("Buckets", [])}:
            s3.create_bucket(Bucket=ARTIFACT_BUCKET)
    except Exception:  # noqa: BLE001 - S3Proxy may not be up; mlflow handles missing later
        pass


def _slim_image(tag: str) -> None:
    """Run docker-slim on the built image (in-place tag) if `slim` is on PATH."""
    if shutil.which("slim") is None:
        return
    full = f"{tag.split(':')[0]}:full"
    subprocess.run(["docker", "tag", tag, full], check=False, capture_output=True)
    subprocess.run(["slim", "build", "--target", full, "--tag", tag,
                    "--http-probe=false", "--continue-after", "10"],
                   check=False, capture_output=True)


class MlflowService(Service):
    """Local MLflow App container, pre-wired to S3Proxy for artifact storage."""

    name = "mlflow"

    def __init__(self, host_port: int = ports.MLFLOW,
                 s3_endpoint: str = "http://host.docker.internal:9000"):
        """Initialize on host_port (5050; 5000 collides with macOS AirPlay)."""
        super().__init__(
            name="mlflow",
            image=IMAGE_TAG,
            ports=[PortMapping(container_port=5050, host_port=host_port)],
            environment={
                "MLFLOW_S3_ENDPOINT_URL": s3_endpoint,
                "AWS_ACCESS_KEY_ID": "test",
                "AWS_SECRET_ACCESS_KEY": "test",
                "AWS_DEFAULT_REGION": config.region(),
                "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
                "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
            },
            volumes={"oblako-mlflow-data": {"bind": "/data", "mode": "rw"}},
            # Reach S3Proxy running on the host (no shared docker network yet).
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
        self.host_port = host_port
        self.s3_endpoint = s3_endpoint

    @property
    def tracking_uri(self) -> str:
        """The URL clients pass to ``mlflow.set_tracking_uri``."""
        return f"http://localhost:{self.host_port}"

    def ensure_image(self) -> None:
        """Build the MLflow image (and slim it if docker-slim is available)."""
        from docker.errors import ImageNotFound

        try:
            self.client.images.get(IMAGE_TAG)
            return
        except ImageNotFound:
            pass
        print(f"Building {IMAGE_TAG} from {_DOCKERFILE_DIR}/Dockerfile...")
        self.client.images.build(path=str(_DOCKERFILE_DIR), tag=IMAGE_TAG, rm=True)
        _slim_image(IMAGE_TAG)

    def start(self) -> None:
        """Build the image (if needed), ensure the artifact bucket, then run."""
        self.ensure_image()
        _ensure_bucket(self.s3_endpoint.replace("host.docker.internal", "localhost"))
        super().start()

    def _health_check(self) -> bool:
        try:
            resp = httpx.get(f"http://localhost:{self.host_port}/health", timeout=3.0)
            return resp.status_code == 200
        except httpx.HTTPError:  # any transport error (incl. accept-then-reset) = not ready
            return False
