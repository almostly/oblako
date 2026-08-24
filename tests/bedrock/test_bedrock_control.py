"""Integration tests for the Bedrock control plane (boto3 'bedrock').

Foundation-model listing needs only the local server. The batch model-invocation
job also needs the Ollama engine and S3Proxy:
    docker compose up -d s3proxy && oblako pull qwen2.5:0.5b
"""

import json
import time

import boto3
import pytest

from oblako.engines.bedrock.ollama_client import OllamaClient
from oblako.engines.bedrock_runtime import start_in_thread
from oblako.services import S3ProxyService

CREDS = dict(
    region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
)


@pytest.fixture(scope="module")
def bedrock():
    url = start_in_thread(port=8015)
    return boto3.client("bedrock", endpoint_url=url, **CREDS)


def test_list_foundation_models(bedrock):
    models = bedrock.list_foundation_models()["modelSummaries"]
    assert len(models) > 10
    ids = {m["modelId"] for m in models}
    assert "anthropic.claude-3-haiku-20240307-v1:0" in ids


def test_get_foundation_model(bedrock):
    detail = bedrock.get_foundation_model(
        modelIdentifier="anthropic.claude-3-5-sonnet-20241022-v2:0"
    )["modelDetails"]
    assert detail["providerName"] == "Anthropic"
    assert detail["modelArn"].endswith("anthropic.claude-3-5-sonnet-20241022-v2:0")


def test_get_foundation_model_invalid(bedrock):
    with pytest.raises(bedrock.exceptions.ValidationException):
        bedrock.get_foundation_model(modelIdentifier="nope.not-a-model")


def test_catalog_advertises_only_serveable_models():
    # invariant: every advertised chat model routes through the OpenRouter backend
    # (the Ollama backend serves them all locally anyway). Keeps the advertised
    # catalog honest and in sync with OPENROUTER_MODEL_MAP.
    from oblako.engines.bedrock.foundation_models import FOUNDATION_MODELS
    from oblako.engines.bedrock.models import resolve_openrouter

    for model_id, detail in FOUNDATION_MODELS.items():
        if "EMBEDDING" in detail["outputModalities"]:
            continue  # embeddings have no OpenRouter chat equivalent
        resolve_openrouter(model_id)  # raises if not serveable

    # the modern families the extract advertises are now present
    ids = set(FOUNDATION_MODELS)
    for expected in (
        "amazon.nova-lite-v1:0",
        "meta.llama3-2-90b-instruct-v1:0",
        "mistral.mistral-large-2402-v1:0",
        "ai21.jamba-1-5-large-v1:0",
    ):
        assert expected in ids


def test_batch_model_invocation_job(bedrock):
    if not OllamaClient().is_available() or not OllamaClient().list_models():
        pytest.skip("Bedrock engine (Ollama) not available with a model")
    s3 = S3ProxyService().get_client()
    try:
        for b in ("bedrock-it-in", "bedrock-it-out"):
            try:
                s3.create_bucket(Bucket=b)
            except Exception:
                pass
        s3.list_buckets()
    except Exception:
        pytest.skip("S3Proxy not available")

    model = OllamaClient().list_models()[0]["name"]
    record = {
        "recordId": "r1",
        "modelInput": {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "Reply with: ok"}],
        },
    }
    s3.put_object(
        Bucket="bedrock-it-in", Key="in/records.jsonl", Body=json.dumps(record).encode()
    )

    job_arn = bedrock.create_model_invocation_job(
        jobName="it-batch",
        roleArn="arn:aws:iam::000000000000:role/Dummy",
        modelId=model,
        inputDataConfig={"s3InputDataConfig": {"s3Uri": "s3://bedrock-it-in/in/"}},
        outputDataConfig={"s3OutputDataConfig": {"s3Uri": "s3://bedrock-it-out/out/"}},
    )["jobArn"]

    for _ in range(60):
        details = bedrock.get_model_invocation_job(jobIdentifier=job_arn)
        if details["status"] in ("Completed", "Failed", "Stopped"):
            break
        time.sleep(1)
    assert details["status"] == "Completed"
    assert details["totalRecordCount"] == 1
    assert details["successRecordCount"] == 1

    keys = [
        o["Key"]
        for o in s3.list_objects_v2(Bucket="bedrock-it-out", Prefix="out/").get(
            "Contents", []
        )
    ]
    assert keys
    out = json.loads(
        s3.get_object(Bucket="bedrock-it-out", Key=keys[0])["Body"]
        .read()
        .splitlines()[0]
    )
    assert out["recordId"] == "r1"
    assert "modelOutput" in out

    assert any(
        j["jobName"] == "it-batch"
        for j in bedrock.list_model_invocation_jobs()["invocationJobSummaries"]
    )
