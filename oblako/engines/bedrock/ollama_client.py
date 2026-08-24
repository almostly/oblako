"""Thin wrapper around Ollama HTTP API."""

import json

import httpx

DEFAULT_OLLAMA_URL = "http://localhost:11434"


class OllamaClient:
    """Thin synchronous HTTP client for the Ollama REST API."""

    def __init__(self, base_url: str = DEFAULT_OLLAMA_URL):
        """Initialize with the Ollama server base URL."""
        self.base_url = base_url.rstrip("/")

    def chat(self, model: str, messages: list[dict], **kwargs) -> dict:
        """Send a chat completion request to Ollama."""
        payload = {"model": model, "messages": messages, "stream": False}
        if "max_tokens" in kwargs:
            payload.setdefault("options", {})["num_predict"] = kwargs["max_tokens"]
        if "temperature" in kwargs:
            payload.setdefault("options", {})["temperature"] = kwargs["temperature"]
        if "top_p" in kwargs:
            payload.setdefault("options", {})["top_p"] = kwargs["top_p"]

        resp = httpx.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()

    def chat_stream(self, model: str, messages: list[dict], **kwargs):
        """Stream a chat completion from Ollama, yielding each JSON chunk."""
        payload = {"model": model, "messages": messages, "stream": True}
        if "max_tokens" in kwargs:
            payload.setdefault("options", {})["num_predict"] = kwargs["max_tokens"]
        if "temperature" in kwargs:
            payload.setdefault("options", {})["temperature"] = kwargs["temperature"]
        if "top_p" in kwargs:
            payload.setdefault("options", {})["top_p"] = kwargs["top_p"]

        with httpx.stream(
            "POST", f"{self.base_url}/api/chat", json=payload, timeout=120.0
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if line.strip():
                    yield json.loads(line)

    def embed(self, model: str, text: str) -> dict:
        """Return an embedding for ``text`` from an Ollama embedding model."""
        resp = httpx.post(
            f"{self.base_url}/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()

    def list_models(self) -> list[dict]:
        """List locally available models."""
        resp = httpx.get(f"{self.base_url}/api/tags", timeout=10.0)
        resp.raise_for_status()
        return resp.json().get("models", [])

    def is_available(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
            return True
        except (httpx.ConnectError, httpx.TimeoutException):
            return False
