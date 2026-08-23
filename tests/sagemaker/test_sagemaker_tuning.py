"""Integration test: SageMaker Automatic Model Tuning (HPO) runs locally.

Requires Docker + S3Proxy. ``create_hyper_parameter_tuning_job`` runs a real
hyperparameter search: each trial is a local training container with a sampled
``alpha``, and the objective (validation MSE) is scraped from the container's
stdout via the job's MetricDefinitions regex. oblako uses Syne Tune's TPE as the
searcher when installed and a built-in random search otherwise, so this test is
green either way. Override OBLAKO_TEST_S3_ENDPOINT to point at an isolated
S3Proxy.
"""

import os
import pathlib
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "sm-hpo-ci"
ROLE = "arn:aws:iam::000000000000:role/oblako"
TUNE_IMAGE_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "examples"
    / "python"
    / "sagemaker"
    / "tune_image"
)
MAX_JOBS = 6


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


def test_tuning_job_searches_and_reports_best():
    try:
        import docker

        docker.from_env().ping()
        _s3().list_buckets()
    except Exception:
        pytest.skip("Docker or S3Proxy not available")

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.services import SageMakerService

    svc = SageMakerService()
    svc.build_image(path=str(TUNE_IMAGE_DIR), tag="oblako-sagemaker-tune:latest")
    s3 = _s3()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass
    s3.put_object(
        Bucket=BUCKET,
        Key="train/train.csv",
        Body=b"\n".join(f"{x},{2 * x + 1}".encode() for x in range(20)),
    )

    sm = svc.get_client()
    tuning = "ci-hpo"
    sm.create_hyper_parameter_tuning_job(
        HyperParameterTuningJobName=tuning,
        HyperParameterTuningJobConfig={
            "Strategy": "Bayesian",
            "HyperParameterTuningJobObjective": {
                "Type": "Minimize",
                "MetricName": "validation:mse",
            },
            "ResourceLimits": {
                "MaxNumberOfTrainingJobs": MAX_JOBS,
                "MaxParallelTrainingJobs": 1,
            },
            "ParameterRanges": {
                "ContinuousParameterRanges": [
                    {
                        "Name": "alpha",
                        "MinValue": "0.001",
                        "MaxValue": "1000.0",
                        "ScalingType": "Logarithmic",
                    }
                ]
            },
        },
        TrainingJobDefinition={
            "AlgorithmSpecification": {
                "TrainingImage": "oblako-sagemaker-tune:latest",
                "TrainingInputMode": "File",
                "MetricDefinitions": [
                    {"Name": "validation:mse", "Regex": r"validation:mse=([0-9.eE+-]+)"}
                ],
            },
            "RoleArn": ROLE,
            "InputDataConfig": [
                {
                    "ChannelName": "train",
                    "DataSource": {
                        "S3DataSource": {
                            "S3Uri": f"s3://{BUCKET}/train/",
                            "S3DataType": "S3Prefix",
                        }
                    },
                }
            ],
            "OutputDataConfig": {"S3OutputPath": f"s3://{BUCKET}/output"},
            "ResourceConfig": {
                "InstanceType": "ml.m5.large",
                "InstanceCount": 1,
                "VolumeSizeInGB": 1,
            },
            "StoppingCondition": {"MaxRuntimeInSeconds": 600},
        },
    )

    status = "InProgress"
    desc = {}
    for _ in range(300):
        desc = sm.describe_hyper_parameter_tuning_job(
            HyperParameterTuningJobName=tuning
        )
        status = desc["HyperParameterTuningJobStatus"]
        if status in ("Completed", "Failed"):
            break
        time.sleep(1)
    assert status == "Completed", desc.get("FailureReason")

    assert desc["TrainingJobStatusCounters"]["Completed"] == MAX_JOBS
    best = desc["BestTrainingJob"]
    best_value = best["FinalHyperParameterTuningJobObjectiveMetric"]["Value"]
    assert best["FinalHyperParameterTuningJobObjectiveMetric"]["MetricName"] == (
        "validation:mse"
    )
    assert "alpha" in best["TunedHyperParameters"]

    # the reported best is the true minimum objective across every trial
    trials = [
        s["TrainingJobName"]
        for s in sm.list_training_jobs()["TrainingJobSummaries"]
        if s["TrainingJobName"].startswith(f"{tuning}-")
    ]
    assert len(trials) == MAX_JOBS
    observed = [
        sm.describe_training_job(TrainingJobName=t)["FinalMetricDataList"][0]["Value"]
        for t in trials
    ]
    assert abs(best_value - min(observed)) < 1e-9

    # the best trial's model artifact was written to S3
    key = f"output/{best['TrainingJobName']}/output/model.tar.gz"
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    summaries = sm.list_hyper_parameter_tuning_jobs()[
        "HyperParameterTuningJobSummaries"
    ]
    assert tuning in [s["HyperParameterTuningJobName"] for s in summaries]
