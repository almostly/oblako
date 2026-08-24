"""Integration test: the SageMaker control plane runs training jobs locally.

Requires Docker + S3Proxy (docker compose up s3proxy). Unmodified boto3 ``sagemaker``
code trains in a real container per the ``/opt/ml`` contract and writes the model
artifact to S3 — oblako runs it all locally. Override OBLAKO_TEST_S3_ENDPOINT to
point at an isolated S3Proxy.
"""

import io
import json
import os
import pathlib
import tarfile
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "sm-ci"
TRAIN_IMAGE_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "examples"
    / "python"
    / "sagemaker"
    / "train_image"
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


@pytest.fixture(scope="module")
def sagemaker():
    try:
        import docker

        docker.from_env().ping()
        _s3().list_buckets()
    except Exception:
        pytest.skip("Docker or S3Proxy not available")

    # the in-engine executor reads AWS_ENDPOINT_URL_S3; align it with the test's
    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.services import SageMakerService

    svc = SageMakerService()
    svc.build_image(path=str(TRAIN_IMAGE_DIR), tag="oblako-sagemaker-train:latest")
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
    return svc.get_client(), s3


def test_create_training_job_trains_and_writes_model(sagemaker):
    sm, s3 = sagemaker
    job = "ci-train"
    sm.create_training_job(
        TrainingJobName=job,
        AlgorithmSpecification={
            "TrainingImage": "oblako-sagemaker-train:latest",
            "TrainingInputMode": "File",
        },
        RoleArn="arn:aws:iam::000000000000:role/x",
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
    for _ in range(120):
        desc = sm.describe_training_job(TrainingJobName=job)
        status = desc["TrainingJobStatus"]
        if status in ("Completed", "Failed"):
            break
        time.sleep(1)
    assert status == "Completed", desc.get("FailureReason")

    artifact = desc["ModelArtifacts"]["S3ModelArtifacts"]
    assert artifact == f"s3://{BUCKET}/output/{job}/output/model.tar.gz"
    body = s3.get_object(Bucket=BUCKET, Key=artifact.split(f"{BUCKET}/", 1)[1])[
        "Body"
    ].read()
    with tarfile.open(fileobj=io.BytesIO(body)) as tar:
        model = json.load(tar.extractfile("model.json"))
    assert abs(model["slope"] - 2.0) < 1e-6  # data was y = 2x + 1
    assert model["rows"] == 20

    names = [
        s["TrainingJobName"] for s in sm.list_training_jobs()["TrainingJobSummaries"]
    ]
    assert job in names
