"""A SageMaker Pipeline (SDK v3) that runs entirely locally against oblako.

Two ProcessingSteps (prepare -> double) composed into a DAG on a LocalPipelineSession
and executed locally: each step runs its script in a container on the /opt/ml/processing
contract, and step-to-step data flows through S3Proxy. No AWS account is touched --
oblako.engines.sagemaker.use_local_stubs() neutralizes the SDK's role/STS validation,
default-bucket lookup, and ECR pull, and AWS_ENDPOINT_URL_S3 points boto3 at S3Proxy.

This is exactly the champion/challenger pipeline pattern from the book, trimmed to two
steps. Prereqs: pip install "oblako[sagemaker]"; Docker + S3Proxy (docker compose up s3proxy).
"""

import os
import pathlib

import boto3
from botocore.config import Config

from oblako.engines.sagemaker import use_local_stubs

HERE = pathlib.Path(__file__).parent
SCRIPTS = HERE / "scripts"
BUCKET = os.environ.get("SCORE_BUCKET", "oblako-pipeline")
IMAGE = os.environ.get("STEP_IMAGE", "oblako-sagemaker-process:latest")
ROLE = os.environ.get("SAGEMAKER_ROLE_ARN", "arn:aws:iam::000000000000:role/local")
os.environ.setdefault("AWS_ENDPOINT_URL_S3", "http://localhost:9000")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

use_local_stubs()  # run with no AWS account

# ensure the bucket exists on S3Proxy: it holds the staged step code and the
# step-to-step I/O (the session's default_bucket, kept by use_local_stubs).
_s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"],
    config=Config(
        s3={"addressing_style": "path"}, request_checksum_calculation="when_required"
    ),
)
if BUCKET not in [b["Name"] for b in _s3.list_buckets()["Buckets"]]:
    _s3.create_bucket(Bucket=BUCKET)

from sagemaker.core.processing import (  # noqa: E402
    PipelineSession,
    ProcessingInput,
    ProcessingOutput,
    ScriptProcessor,
)
from sagemaker.core.shapes import ProcessingS3Input, ProcessingS3Output  # noqa: E402
from sagemaker.mlops.local.local_pipeline_session import (  # noqa: E402
    LocalPipelineSession as _MlopsLocalPipelineSession,
)
from sagemaker.mlops.workflow.pipeline import Pipeline  # noqa: E402
from sagemaker.mlops.workflow.steps import ProcessingStep  # noqa: E402


class LocalPipelineSession(_MlopsLocalPipelineSession, PipelineSession):
    """Both halves v3 splits across two classes: the mlops pipeline methods plus
    being a real PipelineSession, so steps compose into a deferred DAG."""


sess = LocalPipelineSession(default_bucket=BUCKET)


def processor(name):
    return ScriptProcessor(
        image_uri=IMAGE,
        command=["python3"],
        instance_count=1,
        instance_type="local",
        base_job_name=name,
        role=ROLE,
        sagemaker_session=sess,
    )


prepare = ProcessingStep(
    name="prepare",
    step_args=processor("prepare").run(
        code=str(SCRIPTS / "prepare.py"),
        outputs=[
            ProcessingOutput(
                output_name="data",
                s3_output=ProcessingS3Output(
                    s3_uri=f"s3://{BUCKET}/io/prepare/data",
                    local_path="/opt/ml/processing/output",
                    s3_upload_mode="EndOfJob",
                ),
            )
        ],
    ),
)
prepare_uri = prepare.properties.ProcessingOutputConfig.Outputs["data"].S3Output.S3Uri

double = ProcessingStep(
    name="double",
    step_args=processor("double").run(
        code=str(SCRIPTS / "double.py"),
        inputs=[
            ProcessingInput(
                input_name="data",
                s3_input=ProcessingS3Input(
                    s3_uri=prepare_uri,
                    local_path="/opt/ml/processing/input",
                    s3_data_type="S3Prefix",
                    s3_input_mode="File",
                ),
            )
        ],
        outputs=[
            ProcessingOutput(
                output_name="out",
                s3_output=ProcessingS3Output(
                    s3_uri=f"s3://{BUCKET}/io/double/out",
                    local_path="/opt/ml/processing/output",
                    s3_upload_mode="EndOfJob",
                ),
            )
        ],
    ),
)

pipeline = Pipeline(name="oblako-demo", steps=[prepare, double], sagemaker_session=sess)

if __name__ == "__main__":
    # v3 routes pipeline.upsert()/start() through sagemaker_client, which local
    # mode lacks; the local session carries these methods itself.
    sess.create_pipeline(pipeline, "oblako local pipeline demo")
    sess.start_pipeline_execution(PipelineName=pipeline.name)
    result = _s3.get_object(Bucket=BUCKET, Key="io/double/out/doubled.csv")[
        "Body"
    ].read()
    print("pipeline complete; doubled ->", result.decode().replace("\n", ", "))
