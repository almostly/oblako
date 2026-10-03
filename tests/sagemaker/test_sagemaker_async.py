"""Integration test: SageMaker Asynchronous Inference runs locally.

Requires Docker + S3Proxy. An endpoint with an AsyncInferenceConfig accepts
InvokeEndpointAsync: the request points at an S3 input, returns immediately with
an S3 OutputLocation, and oblako runs the serving container in the background and
writes the response to that location — unmodified boto3, all local. Override
OBLAKO_TEST_S3_ENDPOINT to point at an isolated S3Proxy.
"""

import os
import pathlib
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "sm-async-ci"
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


def _wait(fn, key, done, tries=180):
    desc = {}
    for _ in range(tries):
        desc = fn()
        if desc.get(key) in done:
            return desc
        time.sleep(1)
    return desc


def test_invoke_endpoint_async_writes_result_to_s3():
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
        TrainingJobName="async-train",
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
        lambda: sm.describe_training_job(TrainingJobName="async-train"),
        "TrainingJobStatus",
        ("Completed", "Failed"),
    )
    assert trained["TrainingJobStatus"] == "Completed", trained.get("FailureReason")

    sm.create_model(
        ModelName="async-model",
        PrimaryContainer={
            "Image": "oblako-sagemaker-serve:latest",
            "ModelDataUrl": trained["ModelArtifacts"]["S3ModelArtifacts"],
        },
        ExecutionRoleArn=ROLE,
    )
    sm.create_endpoint_config(
        EndpointConfigName="async-cfg",
        ProductionVariants=[
            {
                "VariantName": "v1",
                "ModelName": "async-model",
                "InstanceType": "ml.m5.large",
                "InitialInstanceCount": 1,
            }
        ],
        AsyncInferenceConfig={
            "OutputConfig": {
                "S3OutputPath": f"s3://{BUCKET}/async-out",
                "S3FailurePath": f"s3://{BUCKET}/async-fail",
            }
        },
    )
    sm.create_endpoint(EndpointName="async-ep", EndpointConfigName="async-cfg")
    endpoint = _wait(
        lambda: sm.describe_endpoint(EndpointName="async-ep"),
        "EndpointStatus",
        ("InService", "Failed"),
    )
    assert endpoint["EndpointStatus"] == "InService", endpoint.get("FailureReason")
    assert endpoint["AsyncInferenceConfig"]["OutputConfig"]["S3OutputPath"] == (
        f"s3://{BUCKET}/async-out"
    )

    try:
        s3.put_object(Bucket=BUCKET, Key="async-in/req.csv", Body=b"5\n10\n100")
        rt = svc.get_runtime_client()
        resp = rt.invoke_endpoint_async(
            EndpointName="async-ep",
            InputLocation=f"s3://{BUCKET}/async-in/req.csv",
            ContentType="text/csv",
        )
        assert resp["InferenceId"]
        output_location = resp["OutputLocation"]
        assert output_location.startswith(f"s3://{BUCKET}/async-out/")

        # the result is written to the S3 output location in the background
        out_key = output_location.split(f"{BUCKET}/", 1)[1]
        body = None
        for _ in range(60):
            try:
                body = s3.get_object(Bucket=BUCKET, Key=out_key)["Body"].read()
                break
            except s3.exceptions.NoSuchKey:
                time.sleep(0.5)
        assert body is not None, "async result was not written to S3"
        preds = [float(v) for v in body.decode().split()]
        assert preds == [11.0, 21.0, 201.0]  # model trained on y = 2x + 1
    finally:
        sm.delete_endpoint(EndpointName="async-ep")
