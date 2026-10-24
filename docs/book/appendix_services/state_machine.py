import json
import time

import boto3

definition = {
    "StartAt": "Check",
    "States": {
        "Check": {
            "Type": "Choice",
            "Choices": [
                {
                    "Variable": "$.rows",
                    "NumericGreaterThan": 0,
                    "Next": "Load",
                }
            ],
            "Default": "Skip",
        },
        "Load": {"Type": "Pass", "Result": "loaded", "End": True},
        "Skip": {
            "Type": "Pass",
            "Result": "skipped",
            "End": True,
        },
    },
}
sfn = boto3.client("stepfunctions")
machine = sfn.create_state_machine(
    name="daily-load",
    definition=json.dumps(definition),
    roleArn="arn:aws:iam::123456789012:role/sfn",
)["stateMachineArn"]
for rows in (40, 0):
    run = sfn.start_execution(
        stateMachineArn=machine, input=json.dumps({"rows": rows})
    )["executionArn"]
    while (desc := sfn.describe_execution(executionArn=run))[
        "status"
    ] == "RUNNING":
        time.sleep(0.2)
    print(rows, desc["status"], desc["output"])
