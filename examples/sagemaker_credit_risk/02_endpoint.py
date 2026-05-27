"""Deploy the credit-scoring model to a SageMaker LOCAL endpoint and score applications.

Trains in local mode, then `estimator.deploy(instance_type='local')` spins up a
real serving container (the same image, run as `serve`). The Predictor sends
applications and gets back probability of default, a scorecard score, and an
APPROVE/DECLINE decision — real-time inference, fully local, no cloud.

Prerequisites:
    pip install 'oblako[sagemaker]'
    Docker running
"""

import os
import pathlib
import sys
import tempfile

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

from sagemaker.deserializers import JSONDeserializer
from sagemaker.estimator import Estimator
from sagemaker.serializers import JSONSerializer

from oblako.services import SageMakerService

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import creditdata

HERE = pathlib.Path(__file__).parent
IMAGE = "oblako-credit-risk:latest"

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
print("Training in local mode...")
estimator.fit({"train": f"file://{data_dir}"})

print("Deploying a local endpoint (serving container on :8080)...")
predictor = estimator.deploy(
    initial_instance_count=1, instance_type="local",
    serializer=JSONSerializer(), deserializer=JSONDeserializer(),
)
try:
    apps = creditdata.sample_applications()
    result = predictor.predict({"instances": apps})
    print("\nCredit decisions:")
    for app, pred in zip(apps, result["predictions"]):
        print(f"  income={app['income']:>7} util={app['credit_util']:.2f} "
              f"delinq={app['num_delinquencies']}  ->  PD={pred['pd']:.3f} "
              f"score={pred['score']} {pred['decision']}")
finally:
    predictor.delete_endpoint()
    print("\nEndpoint torn down.")
