"""Integration test for SageMaker local training via oblako's container runner.

Requires Docker. Builds a bring-your-own-container training image and runs it
through ``SageMakerService.run_training`` (SageMaker's ``/opt/ml`` contract, driven
by oblako directly — no SageMaker SDK, so it's version-independent), then checks
the produced model artifact.
"""

import json
import pathlib
import tempfile

import pytest

from oblako.services import SageMakerService

TRAIN_IMAGE_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "examples"
    / "python"
    / "sagemaker"
    / "train_image"
)


@pytest.fixture(scope="module")
def model():
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker not available")

    sm = SageMakerService()
    sm.build_image(path=str(TRAIN_IMAGE_DIR), tag="oblako-sagemaker-train:latest")

    data_dir = tempfile.mkdtemp(prefix="sm-train-")
    with open(f"{data_dir}/train.csv", "w") as f:
        for x in range(20):
            f.write(f"{x},{2 * x + 1}\n")  # y = 2x + 1

    files = sm.run_training(
        image="oblako-sagemaker-train:latest",
        channels={"train": data_dir},
    )
    return json.loads(files["model.json"])


def test_local_training_produces_model(model):
    # oblako ran the BYOC container and collected /opt/ml/model/model.json
    assert abs(model["slope"] - 2.0) < 1e-6
    assert abs(model["intercept"] - 1.0) < 1e-6
    assert model["rows"] == 20
