"""Example 11: SageMaker local-mode training — fully local, no cloud or ECR.

Builds a bring-your-own-container training image, then runs it via SageMaker
local mode (instance_type='local'): a REAL Docker container trains on local
data (file://) and writes a model artifact. No S3, no ECR, no AWS calls.

Prerequisites:
    pip install 'oblako[sagemaker]'
    Docker running
"""

import glob
import json
import os
import pathlib
import tarfile
import tempfile

from sagemaker.estimator import Estimator

from oblako.services import SageMakerService

# Local mode still constructs a boto3 session; give it a region + dummy creds.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

HERE = pathlib.Path(__file__).parent
sm = SageMakerService()

# 1. Build the training image locally (no ECR pull)
image = sm.build_image(path=str(HERE / "sagemaker"), tag="oblako-sagemaker-train:latest")
print("Built image:", image)

# 2. Stage tiny training data (y = 2x + 1) as local files
data_dir = tempfile.mkdtemp(prefix="sm-train-")
with open(os.path.join(data_dir, "train.csv"), "w") as f:
    for x in range(20):
        f.write(f"{x},{2 * x + 1}\n")
out_dir = tempfile.mkdtemp(prefix="sm-out-")

# 3. Train in local mode — launches a real Docker container
estimator = Estimator(
    image_uri="oblako-sagemaker-train:latest",
    role="arn:aws:iam::000000000000:role/dummy",
    instance_count=1,
    instance_type="local",
    sagemaker_session=sm.get_session(),
    output_path=f"file://{out_dir}",
)
estimator.fit({"train": f"file://{data_dir}"})
print("Local training job complete.")

# 4. Inspect the model artifact SageMaker produced
tars = glob.glob(os.path.join(out_dir, "**", "model.tar.gz"), recursive=True)
with tarfile.open(tars[0]) as tar:
    model = json.load(tar.extractfile("model.json"))
print("Model artifact:", model)  # ~ slope 2.0, intercept 1.0
