"""Integration tests for the local Bedrock Runtime (boto3 'bedrock-runtime').

Requires the Bedrock/Ollama engine up with at least one model:
    docker compose up -d bedrock && oblako pull qwen2.5:0.5b
"""

import json
import socket

import boto3
import pytest

from oblako.engines.bedrock.ollama_client import OllamaClient
from oblako.engines.bedrock_runtime import start_in_thread

CREDS = dict(
    region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
)


@pytest.fixture(scope="module")
def model():
    client = OllamaClient()
    if not client.is_available():
        pytest.skip("Bedrock engine (Ollama) not running")
    models = client.list_models()
    if not models:
        pytest.skip("No models pulled into the Bedrock engine")
    return models[0]["name"]


@pytest.fixture(scope="module")
def bedrock():
    # a free port, so the test never collides with a canonical oblako port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = start_in_thread(port=port)
    return boto3.client("bedrock-runtime", endpoint_url=url, **CREDS)


def test_converse(bedrock, model):
    resp = bedrock.converse(
        modelId=model,
        messages=[{"role": "user", "content": [{"text": "Reply with a single word."}]}],
        inferenceConfig={"maxTokens": 16},
    )
    assert resp["output"]["message"]["content"][0]["text"]
    assert resp["usage"]["totalTokens"] >= 0


def test_invoke_model(bedrock, model):
    resp = bedrock.invoke_model(
        modelId=model,
        body=json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ),
    )
    payload = json.loads(resp["body"].read())
    assert payload["content"][0]["text"]


def test_invoke_model_with_response_stream(bedrock, model):
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Say hello."}],
        }
    )
    resp = bedrock.invoke_model_with_response_stream(modelId=model, body=body)
    text, saw_stop = "", False
    for event in resp["body"]:
        chunk = json.loads(event["chunk"]["bytes"])
        if chunk.get("type") == "content_block_delta":
            text += chunk["delta"]["text"]
        if chunk.get("type") == "message_stop":
            saw_stop = True
    assert text.strip()
    assert saw_stop


def test_converse_stream(bedrock, model):
    resp = bedrock.converse_stream(
        modelId=model,
        messages=[{"role": "user", "content": [{"text": "Reply with one word."}]}],
        inferenceConfig={"maxTokens": 16},
    )
    text, stop_reason, usage = "", None, None
    for event in resp["stream"]:
        if "contentBlockDelta" in event:
            text += event["contentBlockDelta"]["delta"]["text"]
        if "messageStop" in event:
            stop_reason = event["messageStop"]["stopReason"]
        if "metadata" in event:
            usage = event["metadata"]["usage"]
    assert text.strip()
    assert stop_reason == "end_turn"
    assert usage["totalTokens"] >= 0
