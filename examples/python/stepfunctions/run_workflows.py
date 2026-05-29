"""Run all Step Functions ASL workflows against local Step Functions.

Deploys each .asl.json file, executes it, and prints the output.

Prerequisites:
    oblako up stepfunctions
"""

import json
import time
from pathlib import Path

import boto3

SF_ENDPOINT = "http://localhost:8083"
DUMMY_ROLE = "arn:aws:iam::012345678901:role/DummyRole"

sfn = boto3.client(
    "stepfunctions",
    endpoint_url=SF_ENDPOINT,
    aws_access_key_id="test",
    aws_secret_access_key="test",
    region_name="us-east-1",
)

ASL_DIR = Path(__file__).parent / "asl"

# -----------------------------------------------------------------------------------------------
# Workflow inputs
# -----------------------------------------------------------------------------------------------
INPUTS = {
    "bedrock-prompt-chaining": {
        "prompts": [
            "What is credit risk?",
            "How do you measure it?",
            "What role does machine learning play?",
        ],
        "total_prompts": 3,
        "prompt_index": 0,
        "conversation_history": [],
    },
    "sagemaker-preprocess-train": {},
    "sagemaker-train-transform": {},
    "sagemaker-hpo-transform": {},
}

# -----------------------------------------------------------------------------------------------
# Deploy and execute
# -----------------------------------------------------------------------------------------------
# Clean up any existing state machines
existing = sfn.list_state_machines()["stateMachines"]
for sm in existing:
    sfn.delete_state_machine(stateMachineArn=sm["stateMachineArn"])
    time.sleep(0.5)

asl_files = sorted(ASL_DIR.glob("*.asl.json"))
print(f"Found {len(asl_files)} ASL workflows\n")

results = {}

for asl_file in asl_files:
    name = asl_file.stem.replace(".asl", "")
    definition = asl_file.read_text()
    workflow_input = INPUTS.get(name, {})

    print(f"{'=' * 60}")
    print(f"Workflow: {name}")
    print(f"{'=' * 60}")

    # Create state machine
    resp = sfn.create_state_machine(
        name=name,
        definition=definition,
        roleArn=DUMMY_ROLE,
    )
    sm_arn = resp["stateMachineArn"]
    print(f"Created: {sm_arn}")

    # Execute
    exec_resp = sfn.start_execution(
        stateMachineArn=sm_arn,
        input=json.dumps(workflow_input),
    )
    exec_arn = exec_resp["executionArn"]
    print(f"Execution: {exec_arn}")

    # Wait for completion
    for _ in range(30):
        desc = sfn.describe_execution(executionArn=exec_arn)
        if desc["status"] != "RUNNING":
            break
        time.sleep(0.5)

    status = desc["status"]
    print(f"Status: {status}")

    if status == "SUCCEEDED":
        output = json.loads(desc["output"])
        print(f"Output: {json.dumps(output, indent=2)}")
        results[name] = {"status": "SUCCEEDED", "output": output}
    else:
        error = desc.get("error", "unknown")
        cause = desc.get("cause", "unknown")
        print(f"Error: {error}")
        print(f"Cause: {cause}")
        results[name] = {"status": status, "error": error, "cause": cause}

    # Get execution history
    history = sfn.get_execution_history(executionArn=exec_arn)
    states_visited = [
        e.get("stateEnteredEventDetails", {}).get("name")
        for e in history["events"]
        if e["type"] == "TaskStateEntered" or e["type"] == "PassStateEntered"
    ]
    print(f"States visited: {' -> '.join(states_visited)}")
    print()

# -----------------------------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------------------------
print(f"{'=' * 60}")
print("Summary")
print(f"{'=' * 60}")
for name, result in results.items():
    print(f"{name}: {result['status']}")

# Cleanup
for sm in sfn.list_state_machines()["stateMachines"]:
    sfn.delete_state_machine(stateMachineArn=sm["stateMachineArn"])
print("\nCleaned up all state machines.")
