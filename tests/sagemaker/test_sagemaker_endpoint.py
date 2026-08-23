"""Integration test: SageMaker real-time endpoints run locally.

Requires Docker + S3Proxy. Trains a model, deploys it to a local serving container
(create_model / create_endpoint_config / create_endpoint), and invokes it through
the sagemaker-runtime API — unmodified boto3, all local. Override
OBLAKO_TEST_S3_ENDPOINT to point at an isolated S3Proxy.
"""

import os
import pathlib
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "sm-ep-ci"
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


@pytest.fixture(scope="module")
def deployed():
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

    sm = svc.get_client()
    sm.create_training_job(
        TrainingJobName="ep-train",
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
        lambda: sm.describe_training_job(TrainingJobName="ep-train"),
        "TrainingJobStatus",
        ("Completed", "Failed"),
    )
    assert trained["TrainingJobStatus"] == "Completed", trained.get("FailureReason")
    artifact = trained["ModelArtifacts"]["S3ModelArtifacts"]

    sm.create_model(
        ModelName="ep-model",
        PrimaryContainer={
            "Image": "oblako-sagemaker-serve:latest",
            "ModelDataUrl": artifact,
        },
        ExecutionRoleArn=ROLE,
    )
    sm.create_endpoint_config(
        EndpointConfigName="ep-cfg",
        ProductionVariants=[
            {
                "VariantName": "v1",
                "ModelName": "ep-model",
                "InstanceType": "ml.m5.large",
                "InitialInstanceCount": 1,
            }
        ],
    )
    sm.create_endpoint(EndpointName="ep-1", EndpointConfigName="ep-cfg")
    endpoint = _wait(
        lambda: sm.describe_endpoint(EndpointName="ep-1"),
        "EndpointStatus",
        ("InService", "Failed"),
    )
    assert endpoint["EndpointStatus"] == "InService", endpoint.get("FailureReason")
    yield svc
    sm.delete_endpoint(EndpointName="ep-1")


def test_invoke_endpoint_returns_predictions(deployed):
    rt = deployed.get_runtime_client()
    resp = rt.invoke_endpoint(
        EndpointName="ep-1", Body=b"5\n10\n100", ContentType="text/csv"
    )
    preds = [float(v) for v in resp["Body"].read().decode().split()]
    # the model trained on y = 2x + 1
    assert preds == [11.0, 21.0, 201.0]
