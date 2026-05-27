"""Live OpenRouter backend test — skipped unless OPENROUTER_API_KEY is set.

Makes ONE small (paid) request to openrouter.ai. Run it by putting the key in
the environment first, e.g.:

    set -a; . ./.env; set +a
    pytest tests/test_bedrock_openrouter_live.py
"""

import os

import pytest

if not os.environ.get("OPENROUTER_API_KEY"):
    pytest.skip("OPENROUTER_API_KEY not set", allow_module_level=True)

from oblako.bedrock.adapter import BedrockAdapter
from oblako.bedrock.backends import OpenRouterBackend


def test_live_converse():
    adapter = BedrockAdapter(backend=OpenRouterBackend(api_key=os.environ["OPENROUTER_API_KEY"]))
    result = adapter.converse(
        model_id="meta.llama3-8b-instruct-v1:0",  # -> meta-llama/llama-3-8b-instruct
        messages=[{"role": "user", "content": [{"text": "Reply with a single word."}]}],
        inference_config={"maxTokens": 16},
    )
    text = result["output"]["message"]["content"][0]["text"]
    assert isinstance(text, str) and text.strip()
    assert result["usage"]["totalTokens"] > 0
