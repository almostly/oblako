"""Step Functions Local for ML workflow orchestration.

Mirrors the pattern from credit-risk-modeling step_functions:
a scoring workflow with choice states for approve/decline.

Prerequisites:
    make up
"""

import json
import time

from oblako.services import StepFunctionsService

sf = StepFunctionsService()
sfn = sf.get_client()

# Define a credit scoring workflow (ASL)
workflow = {
    "Comment": "Credit scoring workflow with approve/decline logic",
    "StartAt": "ScoreApplication",
    "States": {
        "ScoreApplication": {
            "Type": "Pass",
            "Parameters": {
                "customer_id.$": "$.customer_id",
                "score.$": "$.score",
                "threshold": 600,
            },
            "Next": "CheckScore",
        },
        "CheckScore": {
            "Type": "Choice",
            "Choices": [
                {
                    "Variable": "$.score",
                    "NumericGreaterThanEquals": 600,
                    "Next": "Approve",
                },
            ],
            "Default": "Decline",
        },
        "Approve": {
            "Type": "Pass",
            "Result": "APPROVED",
            "ResultPath": "$.decision",
            "Next": "FormatResult",
        },
        "Decline": {
            "Type": "Pass",
            "Result": "DECLINED",
            "ResultPath": "$.decision",
            "Next": "FormatResult",
        },
        "FormatResult": {
            "Type": "Pass",
            "Parameters": {
                "customer_id.$": "$.customer_id",
                "score.$": "$.score",
                "decision.$": "$.decision",
            },
            "End": True,
        },
    },
}

# Create state machine
ROLE = "arn:aws:iam::012345678901:role/DummyRole"
SM_NAME = "credit-scoring-workflow"

# Clean up if exists
try:
    machines = sfn.list_state_machines()["stateMachines"]
    for m in machines:
        if m["name"] == SM_NAME:
            sfn.delete_state_machine(stateMachineArn=m["stateMachineArn"])
except Exception:
    pass

resp = sfn.create_state_machine(
    name=SM_NAME, definition=json.dumps(workflow), roleArn=ROLE
)
sm_arn = resp["stateMachineArn"]
print(f"Created state machine: {SM_NAME}")

# Run test cases
test_cases = [
    {"customer_id": "CUST-001", "score": 720},
    {"customer_id": "CUST-002", "score": 550},
    {"customer_id": "CUST-003", "score": 600},
    {"customer_id": "CUST-004", "score": 480},
]

print(f"\nExecuting {len(test_cases)} applications:")
for case in test_cases:
    exec_resp = sfn.start_execution(stateMachineArn=sm_arn, input=json.dumps(case))
    exec_arn = exec_resp["executionArn"]

    # Wait for completion
    for _ in range(20):
        desc = sfn.describe_execution(executionArn=exec_arn)
        if desc["status"] != "RUNNING":
            break
        time.sleep(0.25)

    output = json.loads(desc["output"])
    print(
        f"{output['customer_id']}: score={output['score']}, decision={output['decision']}"
    )

# Cleanup
sfn.delete_state_machine(stateMachineArn=sm_arn)
print(f"\nCleaned up: {SM_NAME}")
