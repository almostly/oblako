"""Integration test: SageMaker processing jobs run locally.

Requires Docker + S3Proxy. A processing job (the ProcessingStep atom that pipelines
are built from) copies its S3 inputs into a container on the /opt/ml/processing
contract and its outputs back to S3 — unmodified boto3, all local. Override
OBLAKO_TEST_S3_ENDPOINT to point at an isolated S3Proxy.
"""

import os
import pathlib
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "sm-proc-ci"
ROLE = "arn:aws:iam::000000000000:role/oblako"
IMAGE_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "examples"
    / "python"
    / "sagemaker"
    / "process_image"
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


def test_processing_job_reads_and_writes_s3():
    try:
        import docker

        docker.from_env().ping()
        _s3().list_buckets()
    except Exception:
        pytest.skip("Docker or S3Proxy not available")

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.services import SageMakerService

    svc = SageMakerService()
    svc.build_image(path=str(IMAGE_DIR), tag="oblako-sagemaker-process:latest")
    s3 = _s3()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass
    s3.put_object(Bucket=BUCKET, Key="in/data.csv", Body=b"1\n2\n3\n4")

    sm = svc.get_client()
    sm.create_processing_job(
        ProcessingJobName="proc-1",
        AppSpecification={"ImageUri": "oblako-sagemaker-process:latest"},
        RoleArn=ROLE,
        ProcessingInputs=[
            {
                "InputName": "input",
                "S3Input": {
                    "S3Uri": f"s3://{BUCKET}/in/",
                    "LocalPath": "/opt/ml/processing/input",
                    "S3DataType": "S3Prefix",
                    "S3InputMode": "File",
                },
            }
        ],
        ProcessingOutputConfig={
            "Outputs": [
                {
                    "OutputName": "output",
                    "S3Output": {
                        "S3Uri": f"s3://{BUCKET}/out",
                        "LocalPath": "/opt/ml/processing/output",
                        "S3UploadMode": "EndOfJob",
                    },
                }
            ]
        },
        ProcessingResources={
            "ClusterConfig": {
                "InstanceType": "ml.m5.large",
                "InstanceCount": 1,
                "VolumeSizeInGB": 1,
            }
        },
    )

    status = "InProgress"
    desc = {}
    for _ in range(120):
        desc = sm.describe_processing_job(ProcessingJobName="proc-1")
        status = desc["ProcessingJobStatus"]
        if status in ("Completed", "Failed"):
            break
        time.sleep(1)
    assert status == "Completed", desc.get("FailureReason")

    out = s3.get_object(Bucket=BUCKET, Key="out/output.csv")["Body"].read().decode()
    assert [float(v) for v in out.split()] == [2.0, 4.0, 6.0, 8.0]  # each doubled
