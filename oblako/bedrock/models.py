"""Model ID mapping: Bedrock model IDs -> Ollama / OpenRouter models."""

import re

# Small, fast default model for the local Bedrock engine (Ollama).
DEFAULT_MODEL = "qwen2.5:0.5b"

# Map AWS Bedrock model IDs to Ollama-compatible model names.
# Users can override via OBLAKO_MODEL_MAP env var (JSON), or pass an Ollama
# model name directly (resolve_model falls back to the id as-is).
DEFAULT_MODEL_MAP = {
    # Anthropic Claude
    "anthropic.claude-3-haiku-20240307-v1:0": DEFAULT_MODEL,
    "anthropic.claude-3-sonnet-20240229-v1:0": DEFAULT_MODEL,
    "anthropic.claude-3-5-sonnet-20241022-v2:0": DEFAULT_MODEL,
    "anthropic.claude-3-5-haiku-20241022-v1:0": DEFAULT_MODEL,
    # Meta Llama
    "meta.llama3-8b-instruct-v1:0": DEFAULT_MODEL,
    "meta.llama3-70b-instruct-v1:0": DEFAULT_MODEL,
    # Amazon Titan
    "amazon.titan-text-express-v1": DEFAULT_MODEL,
    # Cohere
    "cohere.command-r-plus-v1:0": DEFAULT_MODEL,
}

# Embedding models -> Ollama embedding models
EMBEDDING_MODEL_MAP = {
    "amazon.titan-embed-text-v1": "nomic-embed-text",
    "amazon.titan-embed-text-v2:0": "nomic-embed-text",
    "cohere.embed-english-v3": "nomic-embed-text",
    "cohere.embed-multilingual-v3": "nomic-embed-text",
}


OLLAMA_PREFIX = "ollama."
OPENROUTER_PREFIX = "openrouter."

# Map AWS Bedrock model IDs to OpenRouter slugs. Pinned to OpenRouter's catalog
# (as of 2026-05); where Bedrock's exact version has been retired by OpenRouter
# (Claude 3.x, Llama 2, Mixtral 8x7B, Jurassic, …) it maps to the nearest current
# model in the same family. Context-length variants (…:48k, …:200k) are
# normalized in resolve_openrouter. Titan text maps to Amazon's current text
# model (Nova); embeddings/image models have no chat equivalent (-> clear error).
OPENROUTER_MODEL_MAP = {
    # Anthropic Claude (3.x retired on OpenRouter -> nearest current 4.x)
    "anthropic.claude-instant-v1": "anthropic/claude-3-haiku",
    "anthropic.claude-v2": "anthropic/claude-3.5-haiku",
    "anthropic.claude-v2:1": "anthropic/claude-3.5-haiku",
    "anthropic.claude-3-haiku-20240307-v1:0": "anthropic/claude-3-haiku",
    "anthropic.claude-3-sonnet-20240229-v1:0": "anthropic/claude-sonnet-4.5",
    "anthropic.claude-3-opus-20240229-v1:0": "anthropic/claude-opus-4.5",
    "anthropic.claude-3-5-sonnet-20240620-v1:0": "anthropic/claude-sonnet-4.5",
    "anthropic.claude-3-5-sonnet-20241022-v2:0": "anthropic/claude-sonnet-4.5",
    "anthropic.claude-3-5-haiku-20241022-v1:0": "anthropic/claude-3.5-haiku",
    # Meta Llama (Llama 2 retired -> Llama 3 of the same size)
    "meta.llama2-13b-chat-v1": "meta-llama/llama-3-8b-instruct",
    "meta.llama2-13b-v1": "meta-llama/llama-3-8b-instruct",
    "meta.llama2-70b-chat-v1": "meta-llama/llama-3-70b-instruct",
    "meta.llama2-70b-v1": "meta-llama/llama-3-70b-instruct",
    "meta.llama3-8b-instruct-v1:0": "meta-llama/llama-3-8b-instruct",
    "meta.llama3-70b-instruct-v1:0": "meta-llama/llama-3-70b-instruct",
    "meta.llama3-1-8b-instruct-v1:0": "meta-llama/llama-3.1-8b-instruct",
    "meta.llama3-1-70b-instruct-v1:0": "meta-llama/llama-3.1-70b-instruct",
    "meta.llama3-2-1b-instruct-v1:0": "meta-llama/llama-3.2-1b-instruct",
    "meta.llama3-2-3b-instruct-v1:0": "meta-llama/llama-3.2-3b-instruct",
    "meta.llama3-2-11b-instruct-v1:0": "meta-llama/llama-3.2-11b-vision-instruct",
    "meta.llama3-2-90b-instruct-v1:0": "meta-llama/llama-3.3-70b-instruct",
    # Mistral (8x7B / dated versions retired -> nearest current)
    "mistral.mistral-7b-instruct-v0:2": "mistralai/mistral-7b-instruct-v0.1",
    "mistral.mixtral-8x7b-instruct-v0:1": "mistralai/mixtral-8x22b-instruct",
    "mistral.mistral-large-2402-v1:0": "mistralai/mistral-large-2407",
    "mistral.mistral-small-2402-v1:0": "mistralai/mistral-small-3.2-24b-instruct",
    # Cohere Command
    "cohere.command-text-v14": "cohere/command-a",
    "cohere.command-light-text-v14": "cohere/command-r7b-12-2024",
    "cohere.command-r-v1:0": "cohere/command-r-08-2024",
    "cohere.command-r-plus-v1:0": "cohere/command-r-plus-08-2024",
    # AI21 (Jurassic retired -> Jamba)
    "ai21.j2-mid": "ai21/jamba-large-1.7",
    "ai21.j2-mid-v1": "ai21/jamba-large-1.7",
    "ai21.j2-ultra": "ai21/jamba-large-1.7",
    "ai21.j2-ultra-v1": "ai21/jamba-large-1.7",
    "ai21.j2-grande-instruct": "ai21/jamba-large-1.7",
    "ai21.j2-jumbo-instruct": "ai21/jamba-large-1.7",
    "ai21.jamba-instruct-v1:0": "ai21/jamba-large-1.7",
    "ai21.jamba-1-5-mini-v1:0": "ai21/jamba-large-1.7",
    "ai21.jamba-1-5-large-v1:0": "ai21/jamba-large-1.7",
    # Amazon (Titan text -> Nova, Amazon's current text family; Nova exact)
    "amazon.titan-text-lite-v1": "amazon/nova-micro-v1",
    "amazon.titan-text-express-v1": "amazon/nova-lite-v1",
    "amazon.titan-text-premier-v1:0": "amazon/nova-pro-v1",
    "amazon.nova-micro-v1:0": "amazon/nova-micro-v1",
    "amazon.nova-lite-v1:0": "amazon/nova-lite-v1",
    "amazon.nova-pro-v1:0": "amazon/nova-pro-v1",
    "amazon.nova-premier-v1:0": "amazon/nova-premier-v1",
}

# Strip a Bedrock context-length suffix (e.g. ":48k", ":200k") for map lookup.
_CONTEXT_SUFFIX = re.compile(r":\d+k$")


def resolve_model(bedrock_model_id: str) -> str:
    """Resolve a Bedrock model ID to an Ollama model name.

    Precedence: explicit ``ollama.<name>`` prefix > known Bedrock mapping >
    embedding mapping > the id as-is (so any local Ollama name works directly).
    """
    if bedrock_model_id.startswith(OLLAMA_PREFIX):
        return bedrock_model_id[len(OLLAMA_PREFIX) :]
    model = DEFAULT_MODEL_MAP.get(bedrock_model_id)
    if model:
        return model
    model = EMBEDDING_MODEL_MAP.get(bedrock_model_id)
    if model:
        return model
    # Fallback: use the ID as-is (allows direct Ollama model names)
    return bedrock_model_id


def resolve_openrouter(bedrock_model_id: str) -> str:
    """Resolve a Bedrock model ID to a real OpenRouter model slug.

    Unlike the Ollama backend (every id collapses to one local model), each
    Bedrock id routes to its genuine OpenRouter counterpart, so you test the
    same models Bedrock offers. Precedence:
      ``openrouter.<slug>`` prefix  -> the slug as-is
      known Bedrock mapping          -> the real OpenRouter model
      a raw slug (contains ``/``)    -> passthrough (e.g. anthropic/claude-3.5-sonnet)
      otherwise (unmapped Bedrock id) -> error (no OpenRouter equivalent, e.g. Titan)
    """
    if bedrock_model_id.startswith(OPENROUTER_PREFIX):
        return bedrock_model_id[len(OPENROUTER_PREFIX) :]
    base = _CONTEXT_SUFFIX.sub("", bedrock_model_id)  # drop ":48k"/":200k" variants
    if base in OPENROUTER_MODEL_MAP:
        return OPENROUTER_MODEL_MAP[base]
    if "/" in bedrock_model_id:  # already an OpenRouter slug
        return bedrock_model_id
    raise ValueError(
        f"no OpenRouter equivalent for Bedrock model {bedrock_model_id!r} "
        "(Amazon Titan and embeddings aren't on OpenRouter). Use a mapped Bedrock id, "
        "a raw slug like 'anthropic/claude-3.5-sonnet', or 'openrouter.<slug>'."
    )
