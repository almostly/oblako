"""Bedrock foundation-model registry.

A curated subset of the real Bedrock catalog, in the AWS `FoundationModelSummary`
/ `FoundationModelDetails` schema, so `list_foundation_models` /
`get_foundation_model` return realistic metadata. Locally everything is served
by Ollama, but the model *identities* match AWS.

Pass `modelId="ollama.<name>"` (or just a raw Ollama name) to use any local
model directly — see `OLLAMA_PREFIX` and `resolve_model`.
"""

from __future__ import annotations

OLLAMA_PREFIX = "ollama."

_ON_DEMAND = ["ON_DEMAND"]
_TEXT = ["TEXT"]
_ARN = "arn:{partition}:bedrock:{region}::foundation-model/{model_id}"


def _model(
    model_id,
    name,
    provider,
    *,
    inp=None,
    out=None,
    streaming=True,
    inference=None,
    embedding=False,
):
    """Build a foundation-model detail record (without its ARN)."""
    return {
        "modelId": model_id,
        "modelName": name,
        "providerName": provider,
        "inputModalities": inp or _TEXT,
        "outputModalities": out or (["EMBEDDING"] if embedding else _TEXT),
        "responseStreamingSupported": streaming,
        "customizationsSupported": [],
        "inferenceTypesSupported": inference or _ON_DEMAND,
        "modelLifecycle": {"status": "ACTIVE"},
    }


_TEXT_IMAGE = ["TEXT", "IMAGE"]

# Catalog (modelId -> details, minus the modelArn added per-region). Curated to
# the models oblako can actually serve: every id here routes through a backend
# (the OpenRouter backend maps each to a real model; the Ollama backend serves
# them all locally). Kept in sync with OPENROUTER_MODEL_MAP in models.py.
FOUNDATION_MODELS: dict[str, dict] = {
    m["modelId"]: m
    for m in [
        # Amazon Titan + Nova
        _model("amazon.titan-text-lite-v1", "Titan Text G1 - Lite", "Amazon"),
        _model("amazon.titan-text-express-v1", "Titan Text G1 - Express", "Amazon"),
        _model("amazon.titan-text-premier-v1:0", "Titan Text G1 - Premier", "Amazon"),
        _model("amazon.nova-micro-v1:0", "Nova Micro", "Amazon"),
        _model("amazon.nova-lite-v1:0", "Nova Lite", "Amazon", inp=_TEXT_IMAGE),
        _model("amazon.nova-pro-v1:0", "Nova Pro", "Amazon", inp=_TEXT_IMAGE),
        _model("amazon.nova-premier-v1:0", "Nova Premier", "Amazon", inp=_TEXT_IMAGE),
        _model(
            "amazon.titan-embed-text-v1",
            "Titan Embeddings G1 - Text",
            "Amazon",
            embedding=True,
            streaming=False,
        ),
        _model(
            "amazon.titan-embed-text-v2:0",
            "Titan Text Embeddings V2",
            "Amazon",
            embedding=True,
            streaming=False,
        ),
        # Anthropic Claude
        _model("anthropic.claude-instant-v1", "Claude Instant", "Anthropic"),
        _model("anthropic.claude-v2", "Claude", "Anthropic"),
        _model("anthropic.claude-v2:1", "Claude", "Anthropic"),
        _model(
            "anthropic.claude-3-haiku-20240307-v1:0",
            "Claude 3 Haiku",
            "Anthropic",
            inp=_TEXT_IMAGE,
        ),
        _model(
            "anthropic.claude-3-sonnet-20240229-v1:0",
            "Claude 3 Sonnet",
            "Anthropic",
            inp=_TEXT_IMAGE,
        ),
        _model(
            "anthropic.claude-3-opus-20240229-v1:0",
            "Claude 3 Opus",
            "Anthropic",
            inp=_TEXT_IMAGE,
        ),
        _model(
            "anthropic.claude-3-5-sonnet-20240620-v1:0",
            "Claude 3.5 Sonnet",
            "Anthropic",
            inp=_TEXT_IMAGE,
        ),
        _model(
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "Claude 3.5 Sonnet v2",
            "Anthropic",
            inp=_TEXT_IMAGE,
        ),
        _model(
            "anthropic.claude-3-5-haiku-20241022-v1:0", "Claude 3.5 Haiku", "Anthropic"
        ),
        # Meta Llama
        _model("meta.llama3-8b-instruct-v1:0", "Llama 3 8B Instruct", "Meta"),
        _model("meta.llama3-70b-instruct-v1:0", "Llama 3 70B Instruct", "Meta"),
        _model("meta.llama3-1-8b-instruct-v1:0", "Llama 3.1 8B Instruct", "Meta"),
        _model("meta.llama3-1-70b-instruct-v1:0", "Llama 3.1 70B Instruct", "Meta"),
        _model("meta.llama3-2-1b-instruct-v1:0", "Llama 3.2 1B Instruct", "Meta"),
        _model("meta.llama3-2-3b-instruct-v1:0", "Llama 3.2 3B Instruct", "Meta"),
        _model(
            "meta.llama3-2-11b-instruct-v1:0",
            "Llama 3.2 11B Instruct",
            "Meta",
            inp=_TEXT_IMAGE,
        ),
        _model(
            "meta.llama3-2-90b-instruct-v1:0",
            "Llama 3.2 90B Instruct",
            "Meta",
            inp=_TEXT_IMAGE,
        ),
        # Mistral
        _model("mistral.mistral-7b-instruct-v0:2", "Mistral 7B Instruct", "Mistral AI"),
        _model(
            "mistral.mixtral-8x7b-instruct-v0:1", "Mixtral 8x7B Instruct", "Mistral AI"
        ),
        _model("mistral.mistral-small-2402-v1:0", "Mistral Small", "Mistral AI"),
        _model("mistral.mistral-large-2402-v1:0", "Mistral Large", "Mistral AI"),
        # Cohere
        _model("cohere.command-text-v14", "Command", "Cohere"),
        _model("cohere.command-light-text-v14", "Command Light", "Cohere"),
        _model("cohere.command-r-v1:0", "Command R", "Cohere"),
        _model("cohere.command-r-plus-v1:0", "Command R+", "Cohere"),
        _model(
            "cohere.embed-english-v3",
            "Embed English",
            "Cohere",
            embedding=True,
            streaming=False,
        ),
        _model(
            "cohere.embed-multilingual-v3",
            "Embed Multilingual",
            "Cohere",
            embedding=True,
            streaming=False,
        ),
        # AI21 Jamba
        _model("ai21.jamba-instruct-v1:0", "Jamba-Instruct", "AI21 Labs"),
        _model("ai21.jamba-1-5-mini-v1:0", "Jamba 1.5 Mini", "AI21 Labs"),
        _model("ai21.jamba-1-5-large-v1:0", "Jamba 1.5 Large", "AI21 Labs"),
    ]
}


def _with_arn(detail: dict, region: str, partition: str = "aws") -> dict:
    """Return the model detail with its ``modelArn`` for the region."""
    return {
        **detail,
        "modelArn": _ARN.format(
            partition=partition, region=region, model_id=detail["modelId"]
        ),
    }


def list_models(region: str = "us-east-1") -> list[dict]:
    """All catalog entries as FoundationModelSummary dicts (ARNs filled in)."""
    return [_with_arn(d, region) for d in FOUNDATION_MODELS.values()]


def get_model(model_identifier: str, region: str = "us-east-1") -> dict | None:
    """Return a single FoundationModelDetails dict, or None if unknown."""
    detail = FOUNDATION_MODELS.get(model_identifier)
    return _with_arn(detail, region) if detail else None


def live_model_summary(
    name: str, provider: str = "Ollama", region: str = "us-east-1"
) -> dict:
    """Represent a backend's live model (Ollama tag / OpenRouter slug) as a summary."""
    return _with_arn(_model(name, name, provider.capitalize()), region)
