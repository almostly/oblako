"""Integration test: Data Capture + Model Monitor run locally.

Requires Docker + S3Proxy. An endpoint with DataCaptureConfig logs every
invocation to S3 as SageMaker Data Capture JSON Lines; a monitoring schedule then
runs its analysis as a processing job that reads that captured data and writes a
statistics + constraint-violations report — unmodified boto3, all local. Override
OBLAKO_TEST_S3_ENDPOINT to point at an isolated S3Proxy.
"""

import json
import os
import pathlib
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "sm-mon-ci"
ROLE = "arn:aws:iam::000000000000:role/oblako"
EXAMPLES = (
    pathlib.Path(__file__).resolve().parents[2] / "examples" / "python" / "sagemaker"
)


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


def _wait(fn, key, done, tries=180):
    desc = {}
    for _ in range(tries):
        desc = fn()
        if desc.get(key) in done:
            return desc
        time.sleep(1)
    return desc


def test_data_capture_and_monitoring_schedule():
    try:
        import docker

        docker.from_env().ping()
        _s3().list_buckets()
    except Exception:
        pytest.skip("Docker or S3Proxy not available")

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.services import SageMakerService

    svc = SageMakerService()
    svc.build_image(path=str(EXAMPLES / "train_image"), tag="oblako-sagemaker-train:latest")
    svc.build_image(path=str(EXAMPLES / "serve_image"), tag="oblako-sagemaker-serve:latest")
    svc.build_image(path=str(EXAMPLES / "monitor_image"), tag="oblako-sagemaker-monitor:latest")
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
    rt = svc.get_runtime_client()
    sm.create_training_job(
        TrainingJobName="mon-train",
        AlgorithmSpecification={
            "TrainingImage": "oblako-sagemaker-train:latest",
            "TrainingInputMode": "File",
        },
        RoleArn=ROLE,
        InputDataConfig=[
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
        OutputDataConfig={"S3OutputPath": f"s3://{BUCKET}/output"},
        ResourceConfig={
            "InstanceType": "ml.m5.large",
            "InstanceCount": 1,
            "VolumeSizeInGB": 1,
        },
    )
    trained = _wait(
        lambda: sm.describe_training_job(TrainingJobName="mon-train"),
        "TrainingJobStatus",
        ("Completed", "Failed"),
    )
    assert trained["TrainingJobStatus"] == "Completed", trained.get("FailureReason")
    artifact = trained["ModelArtifacts"]["S3ModelArtifacts"]

    sm.create_model(
        ModelName="mon-model",
        PrimaryContainer={
            "Image": "oblako-sagemaker-serve:latest",
            "ModelDataUrl": artifact,
        },
        ExecutionRoleArn=ROLE,
    )
    capture_uri = f"s3://{BUCKET}/capture"
    sm.create_endpoint_config(
        EndpointConfigName="mon-cfg",
        ProductionVariants=[
            {
                "VariantName": "v1",
                "ModelName": "mon-model",
                "InstanceType": "ml.m5.large",
                "InitialInstanceCount": 1,
            }
        ],
        DataCaptureConfig={
            "EnableCapture": True,
            "InitialSamplingPercentage": 100,
            "DestinationS3Uri": capture_uri,
            "CaptureOptions": [{"CaptureMode": "Input"}, {"CaptureMode": "Output"}],
        },
    )
    sm.create_endpoint(EndpointName="mon-ep", EndpointConfigName="mon-cfg")
    endpoint = _wait(
        lambda: sm.describe_endpoint(EndpointName="mon-ep"),
        "EndpointStatus",
        ("InService", "Failed"),
    )
    assert endpoint["EndpointStatus"] == "InService", endpoint.get("FailureReason")
    assert endpoint["DataCaptureConfig"]["EnableCapture"] is True  # echoed in describe

    try:
        # invocations are captured to S3
        for _ in range(3):
            rt.invoke_endpoint(
                EndpointName="mon-ep", Body=b"5\n10", ContentType="text/csv"
            )

        captured = _wait(
            lambda: {
                "n": len(
                    s3.list_objects_v2(
                        Bucket=BUCKET, Prefix="capture/mon-ep/"
                    ).get("Contents", [])
                )
            },
            "n",
            set(range(3, 100)),  # at least 3 capture files
            tries=30,
        )
        assert captured["n"] >= 3

        # a capture record is the SageMaker captureData JSONL envelope
        first_key = s3.list_objects_v2(Bucket=BUCKET, Prefix="capture/mon-ep/")[
            "Contents"
        ][0]["Key"]
        record = json.loads(
            s3.get_object(Bucket=BUCKET, Key=first_key)["Body"].read().splitlines()[0]
        )
        assert "captureData" in record
        assert record["captureData"]["endpointInput"]["data"].startswith("5")
        assert record["captureData"]["endpointOutput"]["encoding"] == "CSV"

        # a monitoring schedule runs the analyzer as a processing job over the capture
        sm.create_monitoring_schedule(
            MonitoringScheduleName="mon-sched",
            MonitoringScheduleConfig={
                "MonitoringType": "DataQuality",
                "ScheduleConfig": {"ScheduleExpression": "cron(0 * ? * * *)"},
                "MonitoringJobDefinition": {
                    "MonitoringInputs": [
                        {
                            "EndpointInput": {
                                "EndpointName": "mon-ep",
                                "LocalPath": "/opt/ml/processing/input/endpoint",
                            }
                        }
                    ],
                    "MonitoringOutputConfig": {
                        "MonitoringOutputs": [
                            {
                                "S3Output": {
                                    "S3Uri": f"s3://{BUCKET}/monitor-out",
                                    "LocalPath": "/opt/ml/processing/output",
                                }
                            }
                        ]
                    },
                    "MonitoringResources": {
                        "ClusterConfig": {
                            "InstanceType": "ml.m5.large",
                            "InstanceCount": 1,
                            "VolumeSizeInGB": 1,
                        }
                    },
                    "MonitoringAppSpecification": {
                        "ImageUri": "oblako-sagemaker-monitor:latest"
                    },
                    "RoleArn": ROLE,
                },
            },
        )
        sched = _wait(
            lambda: {
                "s": sm.describe_monitoring_schedule(
                    MonitoringScheduleName="mon-sched"
                )
                .get("LastMonitoringExecutionSummary", {})
                .get("MonitoringExecutionStatus")
            },
            "s",
            ("Completed", "Failed"),
            tries=120,
        )
        assert sched["s"] == "Completed"

        stats = json.loads(
            s3.get_object(Bucket=BUCKET, Key="monitor-out/statistics.json")[
                "Body"
            ].read()
        )
        assert stats["num_records"] >= 3  # one record per invocation
        assert stats["num_scores"] >= 6  # two rows per invocation

        names = [
            s["MonitoringScheduleName"]
            for s in sm.list_monitoring_schedules()["MonitoringScheduleSummaries"]
        ]
        assert "mon-sched" in names
    finally:
        sm.delete_endpoint(EndpointName="mon-ep")
