"""The SageMaker Python SDK v3 local modes, end to end, with no AWS account.

The demo notebooks drive SageMaker through the v3 SDK rather than boto3, so this
runs the same paths: ``ModelTrainer`` in ``Mode.LOCAL_CONTAINER``, the
``LocalSession`` endpoint APIs, and the local pipeline example, with
``use_local_stubs()`` standing in for IAM and ECR. Needs Docker (with the compose
plugin, which the SDK's local mode shells out to), the SDK
(``pip install 'oblako[sagemaker]'``), and S3Proxy on :9000 for the pipeline.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path

import pytest

pytest.importorskip("sagemaker.train")

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples/python/sagemaker"
ROLE = "arn:aws:iam::000000000000:role/local"
TRAIN_IMAGE = "oblako-sagemaker-train:latest"
SERVE_IMAGE = "oblako-sagemaker-serve:latest"


@pytest.fixture(scope="module")
def sm():
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker not available")
    for key, value in (
        ("AWS_DEFAULT_REGION", "us-east-1"),
        ("AWS_ACCESS_KEY_ID", "test"),
        ("AWS_SECRET_ACCESS_KEY", "test"),
    ):
        os.environ.setdefault(key, value)
    from oblako.engines.sagemaker import use_local_stubs
    from oblako.services import SageMakerService

    use_local_stubs()
    svc = SageMakerService()
    svc.build_image(path=str(EXAMPLES / "train_image"), tag=TRAIN_IMAGE)
    svc.build_image(path=str(EXAMPLES / "serve_image"), tag=SERVE_IMAGE)
    return svc


@pytest.fixture(scope="module")
def workdir():
    # the SDK bind-mounts its working dir into the container, so keep it under the
    # repo: Docker Desktop shares /Users but not the /var/folders temp dir
    path = Path(__file__).parent / ".sm-local" / uuid.uuid4().hex[:8]
    (path / "train").mkdir(parents=True)
    (path / "train" / "train.csv").write_text(
        "\n".join(f"{x},{2 * x + 1}" for x in range(20)) + "\n"
    )
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="module")
def model_tar(sm, workdir):
    from sagemaker.train import ModelTrainer
    from sagemaker.train.configs import Compute, InputData
    from sagemaker.train.model_trainer import Mode

    root = workdir / "job"
    root.mkdir()
    trainer = ModelTrainer(
        training_image=TRAIN_IMAGE,
        training_mode=Mode.LOCAL_CONTAINER,
        compute=Compute(instance_type="local", instance_count=1),
        role=ROLE,
        local_container_root=str(root),
    )
    trainer.train(
        input_data_config=[
            InputData(channel_name="train", data_source=str(workdir / "train"))
        ]
    )
    return root / "compressed_artifacts" / "model.tar.gz"


def test_model_trainer_local_container_trains(model_tar):
    with tarfile.open(model_tar) as tar:
        model = json.load(tar.extractfile("model.json"))
    assert round(model["slope"], 6) == 2.0
    assert round(model["intercept"], 6) == 1.0
    assert model["rows"] == 20


def test_local_session_endpoint_serves_the_trained_model(model_tar):
    import importlib

    from sagemaker.core.local import LocalSession

    # the local serving path imports sagemaker.serve.model_builder lazily
    importlib.import_module("sagemaker.serve.model_builder")
    with socket.socket() as sock:  # a free host port; the SDK defaults to 8080
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    session = LocalSession()
    session.config = {"local": {"local_code": True, "serving_port": port}}
    client = session.sagemaker_client
    name = f"sdk-v3-{uuid.uuid4().hex[:6]}"
    client.create_model(
        ModelName=name,
        PrimaryContainer={
            "Image": SERVE_IMAGE,
            "ModelDataUrl": f"file://{model_tar}",
            "Environment": {},
        },
    )
    client.create_endpoint_config(
        EndpointConfigName=name,
        ProductionVariants=[
            {
                "VariantName": "AllTraffic",
                "ModelName": name,
                "InitialInstanceCount": 1,
                "InstanceType": "local",
            }
        ],
    )
    client.create_endpoint(EndpointName=name, EndpointConfigName=name)
    try:
        resp = session.sagemaker_runtime_client.invoke_endpoint(
            EndpointName=name, ContentType="application/json", Body="[1, 4]"
        )
        preds = [float(v) for v in resp["Body"].read().decode().split()]
        assert [round(p, 6) for p in preds] == [3.0, 9.0]
    finally:
        client.delete_endpoint(EndpointName=name)
        client.delete_endpoint_config(EndpointConfigName=name)
        client.delete_model(ModelName=name)


def test_local_pipeline_example_runs(sm):
    import boto3

    try:
        boto3.client(
            "s3",
            endpoint_url="http://localhost:9000",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        ).list_buckets()
    except Exception:
        pytest.skip("S3Proxy not reachable on :9000")
    sm.build_image(
        path=str(EXAMPLES / "process_image"), tag="oblako-sagemaker-process:latest"
    )
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "pipeline" / "pipeline_local.py")],
        capture_output=True,
        text=True,
        timeout=900,
        env={**os.environ, "SCORE_BUCKET": f"sdk-v3-{uuid.uuid4().hex[:6]}"},
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert "pipeline complete; doubled ->" in result.stdout
