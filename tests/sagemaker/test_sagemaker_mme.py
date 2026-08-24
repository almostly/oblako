"""Integration test: SageMaker multi-model endpoints (MME).

Requires Docker + S3Proxy. One serving container hosts many models under an S3
prefix; InvokeEndpoint's TargetModel picks which one, loaded on demand. Two
models with different coefficients are served from the same endpoint -
unmodified boto3. Override OBLAKO_TEST_S3_ENDPOINT to point at an isolated
S3Proxy.
"""

import gzip
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
BUCKET = "sm-mme-ci"
ROLE = "arn:aws:iam::000000000000:role/oblako"
IMAGE_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "examples"
    / "python"
    / "sagemaker"
    / "mme_image"
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


def _model_tar(slope, intercept):
    """Build a model.tar.gz whose model.json carries the given coefficients."""
    payload = json.dumps({"slope": slope, "intercept": intercept}).encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo("model.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return gzip.compress(raw.getvalue())


def _wait(fn, key, done, tries=120):
    desc = {}
    for _ in range(tries):
        desc = fn()
        if desc.get(key) in done:
            return desc
        time.sleep(1)
    return desc


def test_multi_model_endpoint_routes_by_target_model():
    try:
        import docker

        docker.from_env().ping()
        _s3().list_buckets()
    except Exception:
        pytest.skip("Docker or S3Proxy not available")

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.services import SageMakerService

    svc = SageMakerService()
    svc.build_image(path=str(IMAGE_DIR), tag="oblako-sagemaker-mme:latest")
    s3 = _s3()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass
    # two models under one prefix: y = 2x + 1  and  y = 3x + 2
    s3.put_object(Bucket=BUCKET, Key="models/a.tar.gz", Body=_model_tar(2, 1))
    s3.put_object(Bucket=BUCKET, Key="models/b.tar.gz", Body=_model_tar(3, 2))

    sm = svc.get_client()
    sm.create_model(
        ModelName="mme-model",
        PrimaryContainer={
            "Image": "oblako-sagemaker-mme:latest",
            "Mode": "MultiModel",
            "ModelDataUrl": f"s3://{BUCKET}/models/",
        },
        ExecutionRoleArn=ROLE,
    )
    sm.create_endpoint_config(
        EndpointConfigName="mme-cfg",
        ProductionVariants=[
            {
                "VariantName": "v1",
                "ModelName": "mme-model",
                "InstanceType": "ml.m5.large",
                "InitialInstanceCount": 1,
            }
        ],
    )
    sm.create_endpoint(EndpointName="mme-ep", EndpointConfigName="mme-cfg")
    endpoint = _wait(
        lambda: sm.describe_endpoint(EndpointName="mme-ep"),
        "EndpointStatus",
        ("InService", "Failed"),
    )
    assert endpoint["EndpointStatus"] == "InService", endpoint.get("FailureReason")

    rt = svc.get_runtime_client()
    try:
        # each TargetModel is loaded on demand and served from the same container
        a = rt.invoke_endpoint(
            EndpointName="mme-ep",
            Body=b"5\n10",
            ContentType="text/csv",
            TargetModel="a.tar.gz",
        )
        assert [float(v) for v in a["Body"].read().decode().split()] == [11.0, 21.0]

        b = rt.invoke_endpoint(
            EndpointName="mme-ep",
            Body=b"5\n10",
            ContentType="text/csv",
            TargetModel="b.tar.gz",
        )
        assert [float(v) for v in b["Body"].read().decode().split()] == [17.0, 32.0]

        # a second call to the first model still works (already loaded)
        a2 = rt.invoke_endpoint(
            EndpointName="mme-ep",
            Body=b"0",
            ContentType="text/csv",
            TargetModel="a.tar.gz",
        )
        assert [float(v) for v in a2["Body"].read().decode().split()] == [1.0]

        # a multi-model endpoint requires a TargetModel
        with pytest.raises(rt.exceptions.ClientError):
            rt.invoke_endpoint(
                EndpointName="mme-ep", Body=b"5", ContentType="text/csv"
            )
    finally:
        sm.delete_endpoint(EndpointName="mme-ep")
