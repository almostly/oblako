"""Integration test: serverless MLflow tracking travels with the model.

Requires Docker + S3Proxy. A training job tracks its run to a sqlite MLflow store
on /opt/ml/model, so the experiment is collected into model.tar.gz and needs no
running MLflow server (the serverless pattern: MLFLOW_TRACKING_URI is a sqlite
path locally, a SageMaker MLflow App ARN on AWS). The test reads the collected
store back with stdlib sqlite3, so it needs no MLflow install. Override
OBLAKO_TEST_S3_ENDPOINT to point at an isolated S3Proxy.
"""

import io
import os
import pathlib
import sqlite3
import tarfile
import tempfile
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "sm-mlflow-ci"
ROLE = "arn:aws:iam::000000000000:role/oblako"
IMAGE_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "examples"
    / "python"
    / "sagemaker"
    / "mlflow_image"
)


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        ),
    )


def test_serverless_mlflow_run_is_collected_with_the_model():
    try:
        import docker

        docker.from_env().ping()
        _s3().list_buckets()
    except Exception:
        pytest.skip("Docker or S3Proxy not available")

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.services import SageMakerService

    svc = SageMakerService()
    svc.build_image(path=str(IMAGE_DIR), tag="oblako-sagemaker-mlflow:latest")
    s3 = _s3()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass
    s3.put_object(
        Bucket=BUCKET,
        Key="train/train.csv",
        Body=b"\n".join(f"{x},{2 * x + 1}".encode() for x in range(20)),
    )

    sm = svc.get_client()
    job = "ci-mlflow"
    sm.create_training_job(
        TrainingJobName=job,
        AlgorithmSpecification={
            "TrainingImage": "oblako-sagemaker-mlflow:latest",
            "TrainingInputMode": "File",
        },
        RoleArn=ROLE,
        # the one env that selects serverless (sqlite) MLflow tracking
        Environment={"MLFLOW_TRACKING_URI": "sqlite:////opt/ml/model/mlruns.db"},
        InputDataConfig=[
            {
                "ChannelName": "train",
                "DataSource": {
                    "S3DataSource": {
                        "S3Uri": f"s3://{BUCKET}/train/",
                        "S3DataType": "S3Prefix",
                    }
                },
            }
        ],
        OutputDataConfig={"S3OutputPath": f"s3://{BUCKET}/output"},
        ResourceConfig={
            "InstanceType": "ml.m5.large",
            "InstanceCount": 1,
            "VolumeSizeInGB": 1,
        },
        StoppingCondition={"MaxRuntimeInSeconds": 600},
    )

    status = "InProgress"
    desc = {}
    for _ in range(180):
        desc = sm.describe_training_job(TrainingJobName=job)
        status = desc["TrainingJobStatus"]
        if status in ("Completed", "Failed"):
            break
        time.sleep(1)
    assert status == "Completed", desc.get("FailureReason")

    # the model artifact carries the MLflow sqlite store + artifacts
    artifact = desc["ModelArtifacts"]["S3ModelArtifacts"]
    body = s3.get_object(Bucket=BUCKET, Key=artifact.split(f"{BUCKET}/", 1)[1])[
        "Body"
    ].read()
    names = []
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(body)) as tar:
            names = tar.getnames()
            tar.extractall(tmp)
        assert "model.json" in names
        assert "mlruns.db" in names  # serverless tracking traveled with the model
        assert any(n.startswith("mlartifacts/") for n in names)  # logged artifact too

        # read the collected store with stdlib sqlite3 (no mlflow needed)
        con = sqlite3.connect(os.path.join(tmp, "mlruns.db"))
        try:
            runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            params = dict(con.execute("SELECT key, value FROM params").fetchall())
            metrics = {k for (k,) in con.execute("SELECT DISTINCT key FROM metrics")}
        finally:
            con.close()
    assert runs == 1
    assert params.get("method") == "least_squares"
    assert params.get("rows") == "20"
    assert {"mse", "slope", "intercept"} <= metrics
