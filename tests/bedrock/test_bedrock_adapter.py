"""Tests for the Bedrock adapter (with a fake backend)."""

import json


class FakeBackend:
    """A chat backend that records calls and returns a canned normalized result."""

    provider = "fake"

    def __init__(self):
        self.calls = []

    def chat(
        self, model_id, messages, *, max_tokens=None, temperature=None, top_p=None
    ):
        self.calls.append(
            {
                "model_id": model_id,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": top_p,
            }
        )
        return {
            "content": "Hello from the model",
            "input_tokens": 10,
            "output_tokens": 5,
        }

    def list_models(self):
        return [{"modelId": "qwen2.5:0.5b", "providerName": "ollama"}]


def _make_adapter():
    from oblako.bedrock.adapter import BedrockAdapter

    return BedrockAdapter(backend=FakeBackend())


def test_invoke_model():
    adapter = _make_adapter()
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
        }
    )
    result = adapter.invoke_model("anthropic.claude-3-haiku-20240307-v1:0", body)
    assert result["role"] == "assistant"
    assert result["content"][0]["text"] == "Hello from the model"
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 5
    assert result["stop_reason"] == "end_turn"


def test_invoke_model_with_system():
    adapter = _make_adapter()
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    )
    adapter.invoke_model("anthropic.claude-3-haiku-20240307-v1:0", body)
    messages = adapter.backend.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": "You are helpful."}
    assert messages[1] == {"role": "user", "content": "Hi"}


def test_converse():
    adapter = _make_adapter()
    result = adapter.converse(
        model_id="anthropic.claude-3-haiku-20240307-v1:0",
        messages=[{"role": "user", "content": [{"text": "Hello"}]}],
        inference_config={"maxTokens": 200, "temperature": 0.7},
    )
    assert result["output"]["message"]["role"] == "assistant"
    assert result["output"]["message"]["content"][0]["text"] == "Hello from the model"
    assert result["stopReason"] == "end_turn"
    assert result["usage"]["totalTokens"] == 15
    # inference config is forwarded to the backend
    assert adapter.backend.calls[0]["max_tokens"] == 200
    assert adapter.backend.calls[0]["temperature"] == 0.7


def test_list_foundation_models():
    adapter = _make_adapter()
    result = adapter.list_foundation_models()
    assert len(result["modelSummaries"]) == 1
    assert result["modelSummaries"][0]["modelId"] == "qwen2.5:0.5b"
    assert result["modelSummaries"][0]["providerName"] == "ollama"
