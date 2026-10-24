import boto3

bedrock = boto3.client("bedrock-runtime")
resp = bedrock.converse(
    modelId="anthropic.claude-3-haiku-20240307-v1:0",
    messages=[
        {
            "role": "user",
            "content": [{"text": "Capital of France, one word?"}],
        }
    ],
    inferenceConfig={"maxTokens": 10, "temperature": 0},
)
print(resp["output"]["message"]["content"][0]["text"])
print(resp["stopReason"], resp["usage"]["inputTokens"] > 0)
