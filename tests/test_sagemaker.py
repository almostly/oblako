"""Integration test for SageMaker local mode.

Requires the sagemaker extra and Docker:
    pip install 'oblako[sagemaker]'

Builds a bring-your-own-container training image and runs a real local-mode
training job (no cloud / ECR), then checks the produced model artifact.
"""

import glob
import json
import os
import pathlib
import tarfile
import tempfile

import pytest

pytest.importorskip("sagemaker")
try:
    from sagemaker.estimator import Estimator
    from sagemaker.local import LocalSession  # noqa: F401
except Exception:  # pragma: no cover - sagemaker v3 has no local mode
    pytest.skip("sagemaker local mode unavailable", allow_module_level=True)

from oblako.services import SageMakerService

EXAMPLE_IMAGE_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "examples" / "sagemaker"
)


@pytest.fixture(scope="module")
def out_dir():
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker not available")

    sm = SageMakerService()
    sm.build_image(path=str(EXAMPLE_IMAGE_DIR), tag="oblako-sagemaker-train:latest")

    data_dir = tempfile.mkdtemp(prefix="sm-train-")
    out = tempfile.mkdtemp(prefix="sm-out-")
    with open(os.path.join(data_dir, "train.csv"), "w") as f:
        for x in range(20):
            f.write(f"{x},{2 * x + 1}\n")

    estimator = Estimator(
        image_uri="oblako-sagemaker-train:latest",
        role="arn:aws:iam::000000000000:role/dummy",
        instance_count=1,
        instance_type="local",
        sagemaker_session=sm.get_session(),
        output_path=f"file://{out}",
    )
    estimator.fit({"train": f"file://{data_dir}"})
    return out


def test_local_training_produces_model(out_dir):
    tars = glob.glob(os.path.join(out_dir, "**", "model.tar.gz"), recursive=True)
    assert tars, "no model.tar.gz produced by local training"
    with tarfile.open(tars[0]) as tar:
        model = json.load(tar.extractfile("model.json"))
    # data was y = 2x + 1
    assert abs(model["slope"] - 2.0) < 1e-6
    assert abs(model["intercept"] - 1.0) < 1e-6
    assert model["rows"] == 20
