"""Integration tests for Step Functions Local (requires: docker compose up stepfunctions)."""

import json
import uuid

import boto3
import pytest

SF_ENDPOINT = "http://localhost:8083"
DUMMY_ROLE = "arn:aws:iam::012345678901:role/DummyRole"


@pytest.fixture
def sfn():
    return boto3.client(
        "stepfunctions",
        endpoint_url=SF_ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )


@pytest.fixture
def state_machine(sfn):
    name = f"test-scoring-{uuid.uuid4().hex[:8]}"
    definition = {
        "Comment": "oblako-ml test workflow",
        "StartAt": "Score",
        "States": {
            "Score": {
                "Type": "Pass",
                "Result": {"score": 0.85, "decision": "APPROVED"},
                "End": True,
            }
        },
    }
    resp = sfn.create_state_machine(
        name=name,
        definition=json.dumps(definition),
        roleArn=DUMMY_ROLE,
    )
    arn = resp["stateMachineArn"]
    yield arn
    sfn.delete_state_machine(stateMachineArn=arn)


def test_create_and_list(sfn, state_machine):
    resp = sfn.list_state_machines()
    arns = [sm["stateMachineArn"] for sm in resp["stateMachines"]]
    assert state_machine in arns


def test_execute_pass_state(sfn, state_machine):
    resp = sfn.start_execution(
        stateMachineArn=state_machine,
        input=json.dumps({"customer_id": "C001"}),
    )
    execution_arn = resp["executionArn"]

    # Poll for completion
    import time
    for _ in range(10):
        desc = sfn.describe_execution(executionArn=execution_arn)
        if desc["status"] != "RUNNING":
            break
        time.sleep(0.5)

    assert desc["status"] == "SUCCEEDED"
    output = json.loads(desc["output"])
    assert output["score"] == 0.85
    assert output["decision"] == "APPROVED"


def test_choice_state(sfn):
    name = f"test-choice-{uuid.uuid4().hex[:8]}"
    definition = {
        "StartAt": "CheckScore",
        "States": {
            "CheckScore": {
                "Type": "Choice",
                "Choices": [
                    {
                        "Variable": "$.score",
                        "NumericGreaterThan": 0.5,
                        "Next": "Approve",
                    }
                ],
                "Default": "Decline",
            },
            "Approve": {
                "Type": "Pass",
                "Result": "APPROVED",
                "ResultPath": "$.decision",
                "End": True,
            },
            "Decline": {
                "Type": "Pass",
                "Result": "DECLINED",
                "ResultPath": "$.decision",
                "End": True,
            },
        },
    }
    resp = sfn.create_state_machine(
        name=name,
        definition=json.dumps(definition),
        roleArn=DUMMY_ROLE,
    )
    arn = resp["stateMachineArn"]

    exec_resp = sfn.start_execution(
        stateMachineArn=arn,
        input=json.dumps({"score": 0.75}),
    )

    import time
    for _ in range(10):
        desc = sfn.describe_execution(executionArn=exec_resp["executionArn"])
        if desc["status"] != "RUNNING":
            break
        time.sleep(0.5)

    assert desc["status"] == "SUCCEEDED"
    output = json.loads(desc["output"])
    assert output["decision"] == "APPROVED"

    sfn.delete_state_machine(stateMachineArn=arn)
