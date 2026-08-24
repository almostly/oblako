"""SageMaker local training — fully local, no cloud, no ECR, no SageMaker SDK.

Builds a bring-your-own-container training image, then runs it through oblako's
container runner (SageMaker's ``/opt/ml`` contract): a REAL Docker container trains
on local data and writes a model artifact. oblako drives Docker itself rather than
the SDK's local mode, so it's independent of the SageMaker SDK version.

Prerequisites:
    pip install oblako      (and Docker running)
"""

import json
import pathlib
import tempfile

from oblako.services import SageMakerService

HERE = pathlib.Path(__file__).parent
sm = SageMakerService()

# 1. Build the training image locally (no ECR pull)
image = sm.build_image(
    path=str(HERE / "train_image"), tag="oblako-sagemaker-train:latest"
)
print("Built image:", image)

# 2. Stage tiny training data (y = 2x + 1) as local files
data_dir = tempfile.mkdtemp(prefix="sm-train-")
with open(f"{data_dir}/train.csv", "w") as f:
    for x in range(20):
        f.write(f"{x},{2 * x + 1}\n")

# 3. Train — oblako runs a real Docker container on the /opt/ml contract, copies
#    the data in, and collects /opt/ml/model back out (no bind-mount sharing needed)
files = sm.run_training(
    image="oblako-sagemaker-train:latest", channels={"train": data_dir}
)

# 4. Inspect the model artifact the container produced
model = json.loads(files["model.json"])
print("Model artifact:", model)  # ~ slope 2.0, intercept 1.0
