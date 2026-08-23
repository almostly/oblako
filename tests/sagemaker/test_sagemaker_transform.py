"""Integration test: SageMaker batch transform runs locally.

Requires Docker + S3Proxy. Trains a model, then runs a batch transform job that
feeds each S3 input object through the model's serving container and writes the
predictions back to S3 — unmodified boto3, all local. Override OBLAKO_TEST_S3_ENDPOINT
to point at an isolated S3Proxy.
"""

import os
import pathlib
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "sm-bt-ci"
ROLE = "arn:aws:iam::000000000000:role/oblako"
EXAMPLES = (
    pathlib.Path(__file__).resolve().parents[2] / "examples" / "python" / "sagemaker"
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


def _wait(fn, key, done, tries=120):
    desc = {}
    for _ in range(tries):
        desc = fn()
        if desc[key] in done:
            return desc
        time.sleep(1)
    return desc


def test_batch_transform_writes_predictions():
    try:
        import docker

        docker.from_env().ping()
        _s3().list_buckets()
    except Exception:
        pytest.skip("Docker or S3Proxy not available")

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.services import SageMakerService

    svc = SageMakerService()
    svc.build_image(
        path=str(EXAMPLES / "train_image"), tag="oblako-sagemaker-train:latest"
    )
    svc.build_image(
        path=str(EXAMPLES / "serve_image"), tag="oblako-sagemaker-serve:latest"
    )
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
    s3.put_object(Bucket=BUCKET, Key="batch/data.csv", Body=b"5\n10\n100")

    sm = svc.get_client()
    sm.create_training_job(
        TrainingJobName="bt-train",
        AlgorithmSpecification={
            "TrainingImage": "oblako-sagemaker-train:latest",
            "TrainingInputMode": "File",
        },
        RoleArn=ROLE,
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
    )
    trained = _wait(
        lambda: sm.describe_training_job(TrainingJobName="bt-train"),
        "TrainingJobStatus",
        ("Completed", "Failed"),
    )
    assert trained["TrainingJobStatus"] == "Completed", trained.get("FailureReason")

    sm.create_model(
        ModelName="bt-model",
        PrimaryContainer={
            "Image": "oblako-sagemaker-serve:latest",
            "ModelDataUrl": trained["ModelArtifacts"]["S3ModelArtifacts"],
        },
        ExecutionRoleArn=ROLE,
    )
    sm.create_transform_job(
        TransformJobName="bt-1",
        ModelName="bt-model",
        TransformInput={
            "DataSource": {
                "S3DataSource": {
                    "S3DataType": "S3Prefix",
                    "S3Uri": f"s3://{BUCKET}/batch/",
                }
            },
            "ContentType": "text/csv",
        },
        TransformOutput={"S3OutputPath": f"s3://{BUCKET}/tout"},
        TransformResources={"InstanceType": "ml.m5.large", "InstanceCount": 1},
    )
    job = _wait(
        lambda: sm.describe_transform_job(TransformJobName="bt-1"),
        "TransformJobStatus",
        ("Completed", "Failed"),
    )
    assert job["TransformJobStatus"] == "Completed", job.get("FailureReason")

    out = s3.get_object(Bucket=BUCKET, Key="tout/data.csv.out")["Body"].read().decode()
    preds = [float(v) for v in out.split()]
    assert preds == [11.0, 21.0, 201.0]  # y = 2x + 1 for x = 5, 10, 100
