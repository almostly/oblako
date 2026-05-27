"""A SageMaker Pipeline (process -> train) run locally against oblako.

Uses `LocalPipelineSession`: a ScriptProcessor step generates the credit data and
a TrainingStep fits the model on its output — the two steps wired together exactly
as on AWS, executing in local-mode Docker containers. Inter-step artifacts pass
through oblako's S3Proxy (the kernel is pointed at it like real S3).

Prerequisites:
    pip install 'oblako[sagemaker]'
    oblako up s3proxy        # S3Proxy on :9000 (artifact store between steps)
    Docker running
"""

import os
import pathlib
import sys
import tempfile

# point SageMaker's boto session at oblako's S3Proxy (path-style + checksum-off)
_cfg = pathlib.Path(tempfile.mkdtemp()) / "aws-config"
_cfg.write_text("[default]\nregion = us-east-1\nrequest_checksum_calculation = when_required\n"
                "s3 =\n    addressing_style = path\n")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ["AWS_ENDPOINT_URL_S3"] = "http://localhost:9000"
os.environ["AWS_REQUEST_CHECKSUM_CALCULATION"] = "when_required"
os.environ["AWS_CONFIG_FILE"] = str(_cfg)

from sagemaker.estimator import Estimator
from sagemaker.processing import ProcessingOutput, ScriptProcessor
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.pipeline_context import LocalPipelineSession
from sagemaker.workflow.steps import ProcessingStep, TrainingStep

from oblako.services import S3ProxyService, SageMakerService

HERE = pathlib.Path(__file__).parent
IMAGE = "oblako-credit-risk:latest"
ROLE = "arn:aws:iam::000000000000:role/dummy"
BUCKET = "credit-risk-pipeline"

SageMakerService().build_image(path=str(HERE / "container"), tag=IMAGE)
s3 = S3ProxyService().get_client()
try:
    s3.create_bucket(Bucket=BUCKET)
except Exception:
    pass

session = LocalPipelineSession()
session.default_bucket = lambda: BUCKET  # pass artifacts through the oblako S3 bucket

# Step 1 — generate the training data (stock python image, no deps)
processor = ScriptProcessor(image_uri="python:3.11-slim", command=["python"],
                            instance_type="local", instance_count=1,
                            role=ROLE, sagemaker_session=session)
prep = ProcessingStep(
    name="GenerateData", processor=processor, code=str(HERE / "prep.py"),
    outputs=[ProcessingOutput(output_name="train", source="/opt/ml/processing/output")],
)

# Step 2 — train on the generated data
estimator = Estimator(
    image_uri=IMAGE, role=ROLE, instance_count=1, instance_type="local",
    sagemaker_session=session,
    hyperparameters={"target-score": "600", "target-odds": "30",
                     "pts-double-odds": "20", "cutoff": "600"},
)
train_step = TrainingStep(
    name="TrainCreditModel", estimator=estimator,
    inputs={"train": prep.properties.ProcessingOutputConfig.Outputs["train"].S3Output.S3Uri},
)

pipeline = Pipeline(name="oblako-credit-risk", steps=[prep, train_step], sagemaker_session=session)
pipeline.upsert(role_arn=ROLE)
print("Running the pipeline locally (process -> train)...")
execution = pipeline.start()  # LocalPipelineSession runs the steps synchronously
print(f"\nPipeline execution: {execution.status}")
for step in execution.list_steps()["PipelineExecutionSteps"]:
    print(f"  {step['StepName']:<18} {step['StepStatus']}")
