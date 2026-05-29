"""Bedrock foundation models + batch model-invocation jobs (local).

Lists the foundation-model catalog via boto3 'bedrock', then runs a batch
inference job: JSONL records in S3 (S3Proxy) -> the local engine (Ollama) ->
JSONL results back in S3. Mirrors real Bedrock batch inference, no cloud.

Prerequisites:
    make up && oblako pull qwen2.5:0.5b
"""

import json
import time

from oblako.services import BedrockService, S3ProxyService

MODEL = "qwen2.5:0.5b"

bedrock = BedrockService().get_control_client()  # boto3.client("bedrock")
s3 = S3ProxyService().get_client()

# 1. Foundation-model catalog (real Bedrock IDs + locally-available Ollama models)
models = bedrock.list_foundation_models()["modelSummaries"]
print(f"Foundation models available: {len(models)}")
for m in models[:5]:
    print(f"{m['modelId']:<45} {m['providerName']}")

# 2. Stage batch input in S3 (clear any prior input so re-runs are deterministic)
for bucket in ("bedrock-batch-in", "bedrock-batch-out"):
    try:
        s3.create_bucket(Bucket=bucket)
    except Exception:
        pass
for obj in s3.list_objects_v2(Bucket="bedrock-batch-in", Prefix="input/").get(
    "Contents", []
):
    s3.delete_object(Bucket="bedrock-batch-in", Key=obj["Key"])

prompts = ["Name one primary color.", "What is 2+2?", "Say hello in French."]
records = [
    {
        "recordId": f"r{i}",
        "modelInput": {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": prompt}],
        },
    }
    for i, prompt in enumerate(prompts)
]
s3.put_object(
    Bucket="bedrock-batch-in",
    Key="input/prompts.jsonl",
    Body="\n".join(json.dumps(r) for r in records).encode(),
)
print(f"\nStaged {len(records)} records to s3://bedrock-batch-in/input/")

# 3. Submit the batch job
job_arn = bedrock.create_model_invocation_job(
    jobName="example-batch",
    roleArn="arn:aws:iam::000000000000:role/Dummy",
    modelId=MODEL,
    inputDataConfig={"s3InputDataConfig": {"s3Uri": "s3://bedrock-batch-in/input/"}},
    outputDataConfig={
        "s3OutputDataConfig": {"s3Uri": "s3://bedrock-batch-out/output/"}
    },
)["jobArn"]
print(f"Submitted job {job_arn.split('/')[-1]}")

# 4. Poll until done
for _ in range(120):
    details = bedrock.get_model_invocation_job(jobIdentifier=job_arn)
    if details["status"] in ("Completed", "Failed", "Stopped"):
        break
    time.sleep(1)
print(
    f"Status: {details['status']} ({details['successRecordCount']}/{details['totalRecordCount']} succeeded)"
)

# 5. Read this job's results back from S3 (output is written under output/<jobId>/)
job_id = job_arn.split("/")[-1]
keys = [
    o["Key"]
    for o in s3.list_objects_v2(
        Bucket="bedrock-batch-out", Prefix=f"output/{job_id}/"
    ).get("Contents", [])
]
print("\nResults:")
for key in keys:
    body = s3.get_object(Bucket="bedrock-batch-out", Key=key)["Body"].read().decode()
    for line in body.splitlines():
        record = json.loads(line)
        if "modelOutput" in record:
            text = record["modelOutput"]["content"][0]["text"].strip()
        else:
            text = f"ERROR: {record.get('error', '')}"
        print(f"{record['recordId']}: {text[:60]}")
