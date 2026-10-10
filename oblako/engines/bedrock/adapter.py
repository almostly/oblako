"""Bedrock API adapter: translate Bedrock invoke_model / converse to a chat backend.

Ollama is the default backend; set OBLAKO_BEDROCK_BACKEND=openrouter to use
OpenRouter instead.
"""

import json
import time

from .backends import make_backend
from .models import EMBEDDING_MODEL_MAP


class BedrockAdapter:
    """Translates Bedrock API calls to a chat backend.

    ``backend`` is any object with ``chat(model_id, messages, *, max_tokens,
    temperature, top_p) -> {"content", "input_tokens", "output_tokens"}`` and
    ``list_models() -> [{"modelId", "providerName"}]``. Defaults to the backend
    selected by env (Ollama unless OBLAKO_BEDROCK_BACKEND=openrouter).
    """

    def __init__(self, backend=None):
        """Initialize with a chat backend, defaulting to the env-configured backend."""
        self.backend = backend or make_backend()

    # -------------------------------------------------------------------------
    # Invoke_model (Anthropic Messages format)
    # -------------------------------------------------------------------------
    def invoke_model(self, model_id: str, body: bytes | str) -> dict:
        """Invoke a model with an Anthropic Messages-format body and return the response dict.

        Embedding models (Titan / Cohere embed, or any ``inputText``/``texts``
        body) return a real vector from the backend instead of a chat message.
        """
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        request = json.loads(body)
        if self._is_embedding_request(model_id, request):
            return self._invoke_embedding(model_id, request)
        messages = self._extract_messages(request)
        result = self.backend.chat(
            model_id,
            messages,
            max_tokens=request.get("max_tokens"),
            temperature=request.get("temperature"),
            top_p=request.get("top_p"),
        )
        return self._format_invoke_response(result, model_id)

    # -------------------------------------------------------------------------
    # Embeddings
    # -------------------------------------------------------------------------
    @staticmethod
    def _is_embedding_request(model_id: str, request: dict) -> bool:
        """Return True if this is an embedding invocation (by model id or body shape)."""
        return (
            "inputText" in request
            or "texts" in request
            or model_id in EMBEDDING_MODEL_MAP
            or "embed" in model_id.lower()
        )

    def _invoke_embedding(self, model_id: str, request: dict) -> dict:
        """Embed the input text and return the model family's response shape."""
        texts = request.get("texts")
        text = (
            " ".join(texts) if isinstance(texts, list) else request.get("inputText", "")
        )
        result = self.backend.embed(model_id, text)
        embedding = result["embedding"]
        if model_id.startswith("cohere."):
            return {
                "embeddings": [embedding],
                "id": f"embed_{int(time.time())}",
                "texts": texts or [text],
                "response_type": "embeddings_floats",
            }
        # Amazon Titan (and default) embedding response shape
        return {
            "embedding": embedding,
            "inputTextTokenCount": result.get("input_tokens", 0),
        }

    def _extract_messages(self, request: dict) -> list[dict]:
        """Extract messages from Bedrock invoke_model request format."""
        messages = []
        if "system" in request:
            system = request["system"]
            if isinstance(system, str):
                messages.append({"role": "system", "content": system})
            elif isinstance(system, list):
                if text := " ".join(
                    b["text"] for b in system if b.get("type") == "text"
                ):
                    messages.append({"role": "system", "content": text})

        for msg in request.get("messages", []):
            role = msg["role"]
            content = msg.get("content", "")
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                text = " ".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                messages.append({"role": role, "content": text})
        return messages

    def _format_invoke_response(self, result: dict, model_id: str) -> dict:
        """Shape a backend result as an Anthropic Messages response."""
        return {
            "id": f"msg_{int(time.time())}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": result["content"]}],
            "model": model_id,
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"],
            },
        }

    # -------------------------------------------------------------------------
    # Streaming
    # -------------------------------------------------------------------------
    def invoke_model_stream(self, model_id: str, body: bytes | str):
        """Stream invoke_model, yielding Anthropic-style chunk dicts.

        Each dict is one streaming event ({message_start, content_block_delta,
        content_block_stop, message_delta, message_stop}); the runtime app frames
        each as a Bedrock ``chunk`` event.
        """
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        request = json.loads(body)
        messages = self._extract_messages(request)
        yield {
            "type": "message_start",
            "message": {"role": "assistant", "content": [], "model": model_id},
        }
        yield {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }
        output_tokens = input_tokens = 0
        for piece in self.backend.chat_stream(
            model_id,
            messages,
            max_tokens=request.get("max_tokens"),
            temperature=request.get("temperature"),
            top_p=request.get("top_p"),
        ):
            if "text" in piece:
                yield {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": piece["text"]},
                }
            if piece.get("done"):
                input_tokens = piece.get("input_tokens", 0)
                output_tokens = piece.get("output_tokens", 0)
        yield {"type": "content_block_stop", "index": 0}
        yield {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": output_tokens},
        }
        yield {
            "type": "message_stop",
            "amazon-bedrock-invocationMetrics": {
                "inputTokenCount": input_tokens,
                "outputTokenCount": output_tokens,
            },
        }

    def converse_stream(
        self,
        model_id: str,
        messages: list[dict],
        system: list[dict] | None = None,
        inference_config: dict | None = None,
    ):
        """Stream converse, yielding (event_type, payload) for each Converse event."""
        chat_messages = []
        if system and (text := " ".join(b.get("text", "") for b in system)):
            chat_messages.append({"role": "system", "content": text})
        for msg in messages:
            text = " ".join(
                b.get("text", "")
                for b in msg.get("content", [])
                if isinstance(b, dict) and "text" in b
            )
            chat_messages.append({"role": msg["role"], "content": text})

        cfg = inference_config or {}
        yield "messageStart", {"role": "assistant"}
        input_tokens = output_tokens = 0
        for piece in self.backend.chat_stream(
            model_id,
            chat_messages,
            max_tokens=cfg.get("maxTokens"),
            temperature=cfg.get("temperature"),
            top_p=cfg.get("topP"),
        ):
            if "text" in piece:
                yield (
                    "contentBlockDelta",
                    {"delta": {"text": piece["text"]}, "contentBlockIndex": 0},
                )
            if piece.get("done"):
                input_tokens = piece.get("input_tokens", 0)
                output_tokens = piece.get("output_tokens", 0)
        yield "contentBlockStop", {"contentBlockIndex": 0}
        yield "messageStop", {"stopReason": "end_turn"}
        yield (
            "metadata",
            {
                "usage": {
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "totalTokens": input_tokens + output_tokens,
                },
                "metrics": {"latencyMs": 0},
            },
        )

    # -------------------------------------------------------------------------
    # Converse
    # -------------------------------------------------------------------------
    def converse(
        self,
        model_id: str,
        messages: list[dict],
        system: list[dict] | None = None,
        inference_config: dict | None = None,
    ) -> dict:
        """Send a Converse API request and return the Bedrock-shaped response dict."""
        chat_messages = []
        if system:
            if text := " ".join(b.get("text", "") for b in system):
                chat_messages.append({"role": "system", "content": text})
        for msg in messages:
            text = " ".join(
                b.get("text", "")
                for b in msg.get("content", [])
                if isinstance(b, dict) and "text" in b
            )
            chat_messages.append({"role": msg["role"], "content": text})

        cfg = inference_config or {}
        result = self.backend.chat(
            model_id,
            chat_messages,
            max_tokens=cfg.get("maxTokens"),
            temperature=cfg.get("temperature"),
            top_p=cfg.get("topP"),
        )
        return self._format_converse_response(result, model_id)

    def _format_converse_response(self, result: dict, model_id: str) -> dict:
        """Shape a backend result as a Bedrock Converse response."""
        input_tokens = result["input_tokens"]
        output_tokens = result["output_tokens"]
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": result["content"]}],
                }
            },
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": input_tokens + output_tokens,
            },
        }

    # -------------------------------------------------------------------------
    # List_foundation_models
    # -------------------------------------------------------------------------
    def list_foundation_models(self) -> dict:
        """Return a ListFoundationModels-shaped dict of all models from the backend."""
        return {
            "modelSummaries": [
                {
                    "modelId": m["modelId"],
                    "modelName": m["modelId"],
                    "providerName": m["providerName"],
                    "inputModalities": ["TEXT"],
                    "outputModalities": ["TEXT"],
                    "modelLifecycle": {"status": "ACTIVE"},
                }
                for m in self.backend.list_models()
            ]
        }
