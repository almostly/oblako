"""Async-inference SNS notifications publish to the configured topic (via moto).

Requires the moto server (SNS + SQS). When an async inference completes or fails,
oblako publishes a SageMaker-shaped notification to the AsyncInferenceConfig
Success/Error topic. This test subscribes an SQS queue to the topic and asserts
the notification is delivered — it exercises the notification path directly, so
it needs neither Docker nor S3Proxy.
"""

import json
import os

import boto3
import pytest


@pytest.fixture(scope="module")
def moto():
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker not available")
    from oblako.services import MotoService

    svc = MotoService()
    try:
        svc.start()
    except Exception as err:  # noqa: BLE001
        pytest.skip(f"moto unavailable: {err}")
    endpoint = svc.endpoint_url
    os.environ["AWS_ENDPOINT_URL_SNS"] = endpoint
    return endpoint


def _client(service, endpoint):
    return boto3.client(
        service,
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_async_completion_publishes_notification(moto):
    sns = _client("sns", moto)
    sqs = _client("sqs", moto)

    topic_arn = sns.create_topic(Name="async-success")["TopicArn"]
    queue_url = sqs.create_queue(QueueName="async-notify")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]
    sns.subscribe(
        TopicArn=topic_arn,
        Protocol="sqs",
        Endpoint=queue_arn,
        Attributes={"RawMessageDelivery": "true"},
    )

    from oblako.engines.sagemaker.executor import SageMakerExecutor

    context = {
        "endpoint_name": "async-ep",
        "inference_id": "req-123",
        "input_location": "s3://bucket/in/req.csv",
        "output_location": "s3://bucket/out/req-123.out",
        "failure_location": "s3://bucket/fail/req-123.out",
        "content_type": "text/csv",
        "notification": {"SuccessTopic": topic_arn},
    }
    SageMakerExecutor._notify_async(context, "Completed")

    messages = []
    for _ in range(20):
        received = sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=1).get(
            "Messages", []
        )
        if received:
            messages = received
            break
    assert messages, "no SNS notification delivered to the queue"
    payload = json.loads(messages[0]["Body"])
    assert payload["invocationStatus"] == "Completed"
    assert payload["inferenceId"] == "req-123"
    assert payload["eventSource"] == "aws:sagemaker"
    assert payload["responseParameters"]["outputLocation"] == (
        "s3://bucket/out/req-123.out"
    )


def test_no_topic_is_a_safe_noop(moto):
    from oblako.engines.sagemaker.executor import SageMakerExecutor

    # no SuccessTopic configured -> nothing published, no error
    SageMakerExecutor._notify_async({"notification": {}}, "Completed")
