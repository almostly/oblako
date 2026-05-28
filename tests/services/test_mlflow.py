"""Unit tests for the MLflow App service (pure config; no container required)."""

from oblako.services.mlflow import (
    ARTIFACT_BUCKET,
    IMAGE_TAG,
    MlflowService,
)


def test_default_port_and_image():
    # 5000 collides with macOS AirPlay Receiver, so the App lands on 5050.
    svc = MlflowService()
    assert svc.host_port == 5050
    assert svc.image == IMAGE_TAG == "oblako-mlflow:latest"
    assert svc.tracking_uri == "http://localhost:5050"


def test_artifact_bucket():
    assert ARTIFACT_BUCKET == "oblako-mlflow"


def test_environment_for_s3proxy():
    svc = MlflowService()
    env = svc.environment
    # MLflow's boto3 uploads use this endpoint (S3Proxy on the host).
    assert env["MLFLOW_S3_ENDPOINT_URL"].endswith(":9000")
    # S3Proxy doesn't implement aws-chunked CRC32 — keep boto3 on the old path.
    assert env["AWS_REQUEST_CHECKSUM_CALCULATION"] == "when_required"
    # Lets the container reach the host where S3Proxy publishes its port.
    assert svc.extra_hosts == {"host.docker.internal": "host-gateway"}


def test_sqlite_persistence_volume():
    svc = MlflowService()
    # MLflow's backend store is SQLite at /data/mlflow.db (per the Dockerfile),
    # backed by a named volume so the experiment history survives restarts.
    assert "oblako-mlflow-data" in svc.volumes
    assert svc.volumes["oblako-mlflow-data"]["bind"] == "/data"
