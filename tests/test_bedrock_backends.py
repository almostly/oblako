"""Unit tests for the Bedrock chat backends (no network/services)."""

import pytest

from oblako.bedrock import backends
from oblako.bedrock.backends import OpenRouterBackend, make_backend
from oblako.bedrock.models import resolve_openrouter


def test_resolve_openrouter():
    # exact OpenRouter slug where the model is still offered
    assert resolve_openrouter("anthropic.claude-3-5-haiku-20241022-v1:0") == "anthropic/claude-3.5-haiku"
    assert resolve_openrouter("meta.llama3-1-70b-instruct-v1:0") == "meta-llama/llama-3.1-70b-instruct"
    assert resolve_openrouter("amazon.nova-pro-v1:0") == "amazon/nova-pro-v1"
    # retired exact version -> nearest current model in the same family
    assert resolve_openrouter("anthropic.claude-3-5-sonnet-20241022-v2:0") == "anthropic/claude-sonnet-4.5"
    assert resolve_openrouter("mistral.mixtral-8x7b-instruct-v0:1") == "mistralai/mixtral-8x22b-instruct"
    assert resolve_openrouter("meta.llama2-70b-chat-v1") == "meta-llama/llama-3-70b-instruct"
    assert resolve_openrouter("amazon.titan-text-express-v1") == "amazon/nova-lite-v1"
    # context-length variants normalize to the base mapping
    assert resolve_openrouter("anthropic.claude-3-haiku-20240307-v1:0:200k") == "anthropic/claude-3-haiku"
    # prefix + raw slug passthrough
    assert resolve_openrouter("openrouter.meta-llama/llama-3-8b-instruct") == "meta-llama/llama-3-8b-instruct"
    assert resolve_openrouter("mistralai/mistral-large-2407") == "mistralai/mistral-large-2407"
    # embeddings / no chat equivalent -> clear error
    with pytest.raises(ValueError):
        resolve_openrouter("amazon.titan-embed-text-v1")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_openrouter_backend_chat(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse({
            "choices": [{"message": {"content": "hi there"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        })

    monkeypatch.setattr(backends.httpx, "post", fake_post)
    backend = OpenRouterBackend(api_key="sk-test")
    out = backend.chat("anthropic.claude-3-5-sonnet-20241022-v2:0",
                       [{"role": "user", "content": "hi"}], max_tokens=64, temperature=0.5)
    assert out == {"content": "hi there", "input_tokens": 7, "output_tokens": 3}
    assert captured["url"].endswith("/chat/completions")
    assert captured["json"]["model"] == "anthropic/claude-sonnet-4.5"  # Bedrock id -> current OpenRouter slug
    assert captured["json"]["max_tokens"] == 64
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_make_backend_default_is_ollama(monkeypatch):
    monkeypatch.delenv("OBLAKO_BEDROCK_BACKEND", raising=False)
    assert make_backend().provider == "ollama"


def test_make_backend_openrouter_requires_key(monkeypatch):
    monkeypatch.setenv("OBLAKO_BEDROCK_BACKEND", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        make_backend()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    assert make_backend().provider == "openrouter"


def test_make_backend_unknown(monkeypatch):
    monkeypatch.setenv("OBLAKO_BEDROCK_BACKEND", "bogus")
    with pytest.raises(RuntimeError):
        make_backend()


def test_adapter_with_openrouter_backend(monkeypatch):
    def fake_post(url, json, headers, timeout):
        return _FakeResponse({
            "choices": [{"message": {"content": "42"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        })

    monkeypatch.setattr(backends.httpx, "post", fake_post)
    from oblako.bedrock.adapter import BedrockAdapter

    adapter = BedrockAdapter(backend=OpenRouterBackend(api_key="sk-test"))
    r = adapter.converse(
        model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
        messages=[{"role": "user", "content": [{"text": "what is 6*7?"}]}],
        inference_config={"maxTokens": 16},
    )
    assert r["output"]["message"]["content"][0]["text"] == "42"
    assert r["usage"]["totalTokens"] == 3
