"""Bedrock Guardrails: create a policy and enforce it with ApplyGuardrail.

Pure in-process (no Ollama/Docker): the guardrail store + ApplyGuardrail
evaluation run in the local Bedrock engine, so unmodified boto3 ``bedrock`` /
``bedrock-runtime`` code can create policies and see GUARDRAIL_INTERVENED.
"""

import boto3
import pytest

from oblako.engines.bedrock_runtime import start_in_thread

CREDS = dict(
    region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
)


@pytest.fixture(scope="module")
def clients():
    url = start_in_thread(port=8016)
    return (
        boto3.client("bedrock", endpoint_url=url, **CREDS),
        boto3.client("bedrock-runtime", endpoint_url=url, **CREDS),
    )


def _apply(runtime, gid, text, source="INPUT"):
    return runtime.apply_guardrail(
        guardrailIdentifier=gid,
        guardrailVersion="DRAFT",
        source=source,
        content=[{"text": {"text": text}}],
    )


def test_guardrail_lifecycle_and_enforcement(clients):
    bedrock, runtime = clients
    created = bedrock.create_guardrail(
        name="content-policy",
        blockedInputMessaging="Sorry, I can't help with that.",
        blockedOutputsMessaging="Sorry, I can't help with that.",
        wordPolicyConfig={"wordsConfig": [{"text": "forbidden"}]},
        topicPolicyConfig={
            "topicsConfig": [
                {
                    "name": "Investment Advice",
                    "definition": "Recommendations to buy or sell securities.",
                    "type": "DENY",
                }
            ]
        },
    )
    gid = created["guardrailId"]
    assert created["guardrailArn"].endswith(gid)
    assert created["version"] == "DRAFT"

    assert bedrock.get_guardrail(guardrailIdentifier=gid)["name"] == "content-policy"
    assert gid in [g["id"] for g in bedrock.list_guardrails()["guardrails"]]

    # clean text passes
    clean = _apply(runtime, gid, "What is the capital of France?")
    assert clean["action"] == "NONE"
    assert clean["outputs"][0]["text"] == "What is the capital of France?"

    # a blocked word intervenes
    blocked = _apply(runtime, gid, "this is forbidden content")
    assert blocked["action"] == "GUARDRAIL_INTERVENED"
    assert blocked["outputs"][0]["text"] == "Sorry, I can't help with that."
    assert blocked["assessments"][0]["wordPolicy"]["customWords"][0]["match"] == (
        "forbidden"
    )

    # a denied topic intervenes (matched by the topic name)
    topic = _apply(runtime, gid, "Please give me some investment advice on stocks")
    assert topic["action"] == "GUARDRAIL_INTERVENED"
    assert topic["assessments"][0]["topicPolicy"]["topics"][0]["name"] == (
        "Investment Advice"
    )

    bedrock.delete_guardrail(guardrailIdentifier=gid)
    with pytest.raises(bedrock.exceptions.ClientError):
        bedrock.get_guardrail(guardrailIdentifier=gid)
