"""Integration tests for Ollama + Bedrock adapter (requires: docker compose up ollama + model pulled)."""

import json

import pytest
from oblako.engines.bedrock.adapter import BedrockAdapter
from oblako.engines.bedrock.backends import OllamaBackend
from oblako.engines.bedrock.ollama_client import OllamaClient


@pytest.fixture
def adapter():
    client = OllamaClient()
    if not client.is_available():
        pytest.skip("Ollama not running")
    if not client.list_models():
        pytest.skip("No models pulled in Ollama")
    return BedrockAdapter(backend=OllamaBackend())


def test_invoke_model_live(adapter):
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Say 'hello' and nothing else."}],
        }
    )
    result = adapter.invoke_model("anthropic.claude-3-haiku-20240307-v1:0", body)
    text = result["content"][0]["text"].lower()
    assert "hello" in text


def test_converse_live(adapter):
    result = adapter.converse(
        model_id="anthropic.claude-3-haiku-20240307-v1:0",
        messages=[
            {"role": "user", "content": [{"text": "Reply with just the word 'yes'."}]}
        ],
        inference_config={"maxTokens": 16},
    )
    text = result["output"]["message"]["content"][0]["text"].lower()
    assert "yes" in text


def test_list_models_live(adapter):
    result = adapter.list_foundation_models()
    assert len(result["modelSummaries"]) > 0
    assert result["modelSummaries"][0]["providerName"] == "ollama"


def test_invoke_embedding_live(adapter):
    # find a pulled embedding model and invoke it via the ollama. passthrough
    embed = next(
        (m["name"] for m in OllamaClient().list_models() if "embed" in m["name"]),
        None,
    )
    if embed is None:
        pytest.skip("No embedding model pulled in Ollama")
    result = adapter.invoke_model(
        f"ollama.{embed}", json.dumps({"inputText": "vector search"})
    )
    assert isinstance(result["embedding"], list)
    assert len(result["embedding"]) > 0
    assert all(isinstance(v, (int, float)) for v in result["embedding"])
