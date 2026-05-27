"""Train a credit-scoring model in SageMaker local mode, store it in oblako's S3.

Builds a bring-your-own-container, trains it via SageMaker **local mode** (a real
Docker container, instance_type='local') on synthetic credit data, then uploads
the resulting model.tar.gz to oblako's **S3Proxy** — the train-and-store-to-S3
flow you'd run on AWS, fully local.

Prerequisites:
    pip install 'oblako[sagemaker]'
    oblako up s3proxy        # S3Proxy on :9000
    Docker running
"""

import glob
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

from sagemaker.estimator import Estimator

from oblako.services import S3ProxyService, SageMakerService

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import creditdata

HERE = pathlib.Path(__file__).parent
IMAGE = "oblako-credit-risk:latest"
BUCKET = "credit-risk-models"


def train():
    """Build the container, train in local mode, and return the model.tar.gz path."""
    sm = SageMakerService()
    print("Building the credit-risk container...")
    sm.build_image(path=str(HERE / "container"), tag=IMAGE)

    data_dir = tempfile.mkdtemp(prefix="credit-train-")
    creditdata.write_training_csv(os.path.join(data_dir, "train.csv"), n=500)
    out_dir = tempfile.mkdtemp(prefix="credit-out-")

    estimator = Estimator(
        image_uri=IMAGE,
        role="arn:aws:iam::000000000000:role/dummy",
        instance_count=1,
        instance_type="local",
        sagemaker_session=sm.get_session(),
        output_path=f"file://{out_dir}",
        hyperparameters={"target-score": "600", "target-odds": "30",
                         "pts-double-odds": "20", "cutoff": "600"},
    )
    print("Training in SageMaker local mode...")
    estimator.fit({"train": f"file://{data_dir}"})
    return glob.glob(os.path.join(out_dir, "**", "model.tar.gz"), recursive=True)[0]


if __name__ == "__main__":
    model_tar = train()
    print("Training complete:", model_tar)

    s3 = S3ProxyService().get_client()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except Exception:
        pass
    s3.upload_file(model_tar, BUCKET, "credit-risk/model.tar.gz")
    print(f"Model stored in oblako S3: s3://{BUCKET}/credit-risk/model.tar.gz")
    print("bucket contents:", [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])])
