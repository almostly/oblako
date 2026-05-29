"""Bedrock invoke_model and converse via Ollama.

Mirrors the pattern from name-mash/bedrock_example.py but runs locally.
No AWS credentials needed.

Prerequisites:
    make up && make ollama-pull
"""

import json
from oblako.engines.bedrock.adapter import BedrockAdapter

adapter = BedrockAdapter()

# -------------------------------------------------------------------------------
# Invoke model (Anthropic Messages format, same as real Bedrock)
# -------------------------------------------------------------------------------
body = json.dumps(
    {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": "What is credit risk? One sentence."},
        ],
    }
)

print("invoke_model:")
result = adapter.invoke_model("anthropic.claude-3-haiku-20240307-v1:0", body)
print(f"  {result['content'][0]['text']}")
print(
    f"  tokens: {result['usage']['input_tokens']} in, {result['usage']['output_tokens']} out"
)

# -------------------------------------------------------------------------------
# Converse (higher-level API)
# -------------------------------------------------------------------------------
print("\nconverse:")
result = adapter.converse(
    model_id="anthropic.claude-3-haiku-20240307-v1:0",
    messages=[
        {
            "role": "user",
            "content": [{"text": "What is a credit scorecard? One sentence."}],
        },
    ],
    system=[{"text": "You are a risk modeling expert."}],
    inference_config={"maxTokens": 64, "temperature": 0.3},
)
print(f"  {result['output']['message']['content'][0]['text']}")
print(
    f"  tokens: {result['usage']['inputTokens']} in, {result['usage']['outputTokens']} out"
)

# -------------------------------------------------------------------------------
# List models
# -------------------------------------------------------------------------------
print("\nAvailable models:")
models = adapter.list_foundation_models()
for m in models["modelSummaries"]:
    print(f"  {m['modelId']}")
