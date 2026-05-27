"""Example 8: a local Bedrock AgentCore agent, fully offline.

The agent runs on the AgentCore Runtime contract (POST /invocations, GET /ping)
and calls oblako's local Bedrock (boto3 'bedrock-runtime' -> Ollama). No cloud.

Run it:
    pip install 'oblako[agentcore]'
    make up && oblako pull qwen2.5:0.5b
    oblako bedrock-runtime &                                  # local Bedrock on :8004
    oblako agentcore run examples/08_agentcore_agent.py       # serves agent on :8080
    oblako agentcore invoke '{"prompt": "Explain credit risk in one sentence."}'
"""

import boto3

from oblako.agentcore import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

bedrock = boto3.client(
    "bedrock-runtime",
    endpoint_url="http://localhost:8004",
    region_name="us-east-1",
    aws_access_key_id="test",
    aws_secret_access_key="test",
)


@app.entrypoint
def handler(payload):
    """Answer a prompt using the local Bedrock model."""
    prompt = payload.get("prompt", "Hello")
    response = bedrock.converse(
        modelId=payload.get("modelId", "qwen2.5:0.5b"),
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 256},
    )
    return {"reply": response["output"]["message"]["content"][0]["text"]}


if __name__ == "__main__":
    app.run()
