"""Chat backends behind the Bedrock adapter.

A backend turns a Bedrock model id + plain messages into a normalized result:
    {"content": str, "input_tokens": int, "output_tokens": int}

  * OllamaBackend     - local, offline (the default).
  * OpenRouterBackend - real frontier models via openrouter.ai (OpenAI-compatible
                        API) using the user's key, so you can test Bedrock-style
                        code against real models without AWS.

Selected via env: OBLAKO_BEDROCK_BACKEND=ollama|openrouter (+ OPENROUTER_API_KEY).
"""

from __future__ import annotations

import os

import httpx

from .models import resolve_model, resolve_openrouter
from .ollama_client import OllamaClient


class OllamaBackend:
    """Chat backend that delegates to a local Ollama instance."""

    provider = "ollama"

    def __init__(self, base_url: str = "http://localhost:11434"):
        """Initialize with the Ollama server base URL."""
        self.client = OllamaClient(base_url)

    def chat(
        self, model_id, messages, *, max_tokens=None, temperature=None, top_p=None
    ) -> dict:
        """Send a chat request to Ollama and return a normalized result dict."""
        kwargs = {}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        result = self.client.chat(resolve_model(model_id), messages, **kwargs)
        return {
            "content": result.get("message", {}).get("content", ""),
            "input_tokens": result.get("prompt_eval_count", 0),
            "output_tokens": result.get("eval_count", 0),
        }

    def embed(self, model_id, text: str) -> dict:
        """Return ``{"embedding": [...], "input_tokens": int}`` from Ollama."""
        result = self.client.embed(resolve_model(model_id), text)
        embedding = result.get("embedding", [])
        return {
            "embedding": embedding,
            "input_tokens": result.get("prompt_eval_count") or len(text.split()),
        }

    def list_models(self) -> list[dict]:
        """Return a list of locally available Ollama models as modelId/providerName dicts."""
        return [
            {"modelId": m["name"], "providerName": self.provider}
            for m in self.client.list_models()
        ]


class OpenRouterBackend:
    """Chat backend that forwards requests to OpenRouter's OpenAI-compatible API."""

    provider = "openrouter"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 120.0,
    ):
        """Initialize with the OpenRouter API key and optional base URL and timeout."""
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat(
        self, model_id, messages, *, max_tokens=None, temperature=None, top_p=None
    ) -> dict:
        """Send a chat completion request to OpenRouter and return a normalized result dict."""
        payload = {"model": resolve_openrouter(model_id), "messages": messages}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/oblako",
                "X-Title": "oblako",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        return {
            "content": data["choices"][0]["message"]["content"],
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }

    def embed(self, model_id, text: str) -> dict:
        """Embeddings aren't served via OpenRouter; use the Ollama backend."""
        raise RuntimeError(
            "the OpenRouter backend does not serve embeddings; use the Ollama "
            "backend (unset OBLAKO_BEDROCK_BACKEND) for invoke_model on embed models"
        )

    def list_models(self) -> list[dict]:
        """Return the configured OpenRouter model map as modelId/providerName dicts."""
        from .models import OPENROUTER_MODEL_MAP

        return [
            {"modelId": bid, "providerName": self.provider}
            for bid in OPENROUTER_MODEL_MAP
        ]


def make_backend(ollama_url: str | None = None):
    """Build the chat backend from env (OBLAKO_BEDROCK_BACKEND, OPENROUTER_API_KEY)."""
    backend = os.environ.get("OBLAKO_BEDROCK_BACKEND", "ollama").lower()
    if backend == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OBLAKO_BEDROCK_BACKEND=openrouter requires OPENROUTER_API_KEY"
            )
        return OpenRouterBackend(api_key=api_key)
    if backend != "ollama":
        raise RuntimeError(
            f"unknown OBLAKO_BEDROCK_BACKEND={backend!r} (expected 'ollama' or 'openrouter')"
        )
    return OllamaBackend(
        base_url=ollama_url
        or os.environ.get("OBLAKO_OLLAMA_URL", "http://localhost:11434")
    )
