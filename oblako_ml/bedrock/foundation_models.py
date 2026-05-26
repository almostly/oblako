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


def _model(model_id, name, provider, *, inp=None, out=None, streaming=True,
           inference=None, embedding=False):
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


# Curated catalog (modelId -> details, minus the modelArn which is added per-region).
FOUNDATION_MODELS: dict[str, dict] = {
    m["modelId"]: m
    for m in [
        # Amazon Titan
        _model("amazon.titan-text-express-v1", "Titan Text G1 - Express", "Amazon"),
        _model("amazon.titan-text-premier-v1:0", "Titan Text G1 - Premier", "Amazon"),
        _model("amazon.titan-embed-text-v1", "Titan Embeddings G1 - Text", "Amazon",
               embedding=True, streaming=False),
        _model("amazon.titan-embed-text-v2:0", "Titan Text Embeddings V2", "Amazon",
               embedding=True, streaming=False),
        # Anthropic Claude
        _model("anthropic.claude-3-haiku-20240307-v1:0", "Claude 3 Haiku", "Anthropic",
               inp=["TEXT", "IMAGE"]),
        _model("anthropic.claude-3-sonnet-20240229-v1:0", "Claude 3 Sonnet", "Anthropic",
               inp=["TEXT", "IMAGE"]),
        _model("anthropic.claude-3-opus-20240229-v1:0", "Claude 3 Opus", "Anthropic",
               inp=["TEXT", "IMAGE"]),
        _model("anthropic.claude-3-5-sonnet-20241022-v2:0", "Claude 3.5 Sonnet v2", "Anthropic",
               inp=["TEXT", "IMAGE"]),
        _model("anthropic.claude-3-5-haiku-20241022-v1:0", "Claude 3.5 Haiku", "Anthropic"),
        # Meta Llama
        _model("meta.llama3-8b-instruct-v1:0", "Llama 3 8B Instruct", "Meta"),
        _model("meta.llama3-70b-instruct-v1:0", "Llama 3 70B Instruct", "Meta"),
        # Mistral
        _model("mistral.mistral-7b-instruct-v0:2", "Mistral 7B Instruct", "Mistral AI"),
        _model("mistral.mixtral-8x7b-instruct-v0:1", "Mixtral 8x7B Instruct", "Mistral AI"),
        # Cohere
        _model("cohere.command-r-plus-v1:0", "Command R+", "Cohere"),
        _model("cohere.embed-english-v3", "Embed English", "Cohere",
               embedding=True, streaming=False),
    ]
}


def _with_arn(detail: dict, region: str, partition: str = "aws") -> dict:
    return {
        **detail,
        "modelArn": _ARN.format(partition=partition, region=region, model_id=detail["modelId"]),
    }


def list_models(region: str = "us-east-1") -> list[dict]:
    """All catalog entries as FoundationModelSummary dicts (ARNs filled in)."""
    return [_with_arn(d, region) for d in FOUNDATION_MODELS.values()]


def get_model(model_identifier: str, region: str = "us-east-1") -> dict | None:
    """A single FoundationModelDetails dict, or None if unknown."""
    detail = FOUNDATION_MODELS.get(model_identifier)
    return _with_arn(detail, region) if detail else None


def live_model_summary(name: str, provider: str = "Ollama", region: str = "us-east-1") -> dict:
    """Represent a backend's live model (Ollama tag / OpenRouter slug) as a summary."""
    return _with_arn(_model(name, name, provider.capitalize()), region)
