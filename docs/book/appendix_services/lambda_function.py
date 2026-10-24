import io
import json
import zipfile

import boto3

HANDLER = """
def handler(event, context):
    total = sum(item["amount"] for item in event["orders"])
    return {"orders": len(event["orders"]), "total": total}
"""

package = io.BytesIO()
with zipfile.ZipFile(package, "w") as archive:
    archive.writestr("app.py", HANDLER)

# Lambda needs a role it may assume, on oblako as on AWS
role = boto3.client("iam").create_role(
    RoleName="order-total",
    AssumeRolePolicyDocument=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": "lambda.amazonaws.com"
                    },
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    ),
)["Role"]["Arn"]

lam = boto3.client("lambda")
lam.create_function(
    FunctionName="order-total",
    Runtime="python3.12",
    Role=role,
    Handler="app.handler",
    Code={"ZipFile": package.getvalue()},
)
lam.get_waiter("function_active_v2").wait(
    FunctionName="order-total"
)
event = {"orders": [{"amount": 12.5}, {"amount": 7.5}]}
resp = lam.invoke(
    FunctionName="order-total", Payload=json.dumps(event)
)
print(resp["StatusCode"], json.loads(resp["Payload"].read()))
