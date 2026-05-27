"""AWS::Serverless-2016-10-31 (SAM) macro expansion.

Real CloudFormation expands the SAM transform server-side during change-set
creation; we do the same so `sam deploy` (and any template with
`Transform: AWS::Serverless-2016-10-31`) reduces to base CFN resource types our
providers know how to provision. Scope: the resources oblako can back —
Function (-> Lambda::Function + IAM::Role, in moto), SimpleTable (-> DynamoDB
Table, real in DynamoDB Local), Api (-> ApiGateway::RestApi, in moto). Event
sources (implicit APIs, permissions) are not wired.
"""

from __future__ import annotations

import copy

SAM_TRANSFORM = "AWS::Serverless-2016-10-31"

_DDB_TYPE = {"String": "S", "Number": "N", "Binary": "B"}

LAMBDA_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}


def is_sam(template: dict) -> bool:
    """Return True if the template declares the SAM transform."""
    t = template.get("Transform")
    transforms = t if isinstance(t, list) else [t]
    return SAM_TRANSFORM in transforms


def transform_sam(template: dict) -> dict:
    """Return a copy of the template with SAM resources expanded to base CFN."""
    template = copy.deepcopy(template)
    expanded: dict = {}
    has_api_event = False
    has_explicit_api = False
    for logical_id, res in template.get("Resources", {}).items():
        rtype = res.get("Type", "")
        props = res.get("Properties", {})
        if rtype == "AWS::Serverless::Function":
            expanded.update(_expand_function(logical_id, props))
            events = props.get("Events") or {}
            if any((e or {}).get("Type") in ("Api", "HttpApi") for e in events.values()):
                has_api_event = True
        elif rtype == "AWS::Serverless::SimpleTable":
            expanded[logical_id] = _expand_simple_table(props)
        elif rtype in ("AWS::Serverless::Api", "AWS::Serverless::HttpApi"):
            expanded[logical_id] = {"Type": "AWS::ApiGateway::RestApi",
                                    "Properties": {"Name": props.get("Name")}}
            has_explicit_api = True
        else:
            if rtype == "AWS::ApiGateway::RestApi":
                has_explicit_api = True
            expanded[logical_id] = res  # already a base CFN resource
    # SAM creates an implicit RestApi for function Api events when none is declared.
    if has_api_event and not has_explicit_api and "ServerlessRestApi" not in expanded:
        expanded["ServerlessRestApi"] = {"Type": "AWS::ApiGateway::RestApi",
                                         "Properties": {"Name": "ServerlessRestApi"}}
    template["Resources"] = expanded
    template.pop("Transform", None)
    return template


def _expand_function(logical_id: str, props: dict) -> dict:
    role_id = f"{logical_id}Role"
    role = {
        "Type": "AWS::IAM::Role",
        "Properties": {
            "RoleName": f"{logical_id}Role",
            "AssumeRolePolicyDocument": LAMBDA_TRUST_POLICY,
        },
    }
    fn_props = {
        "Handler": props.get("Handler", "app.handler"),
        "Runtime": props.get("Runtime", "python3.12"),
        "Role": {"Fn::GetAtt": [role_id, "Arn"]},
        # CodeUri is carried through for traceability; the provider stores a
        # placeholder in moto (real execution stays in `sam local`).
        "CodeUri": props.get("CodeUri", ""),
    }
    for key in ("FunctionName", "Timeout", "MemorySize", "Environment"):
        if key in props:
            fn_props[key] = props[key]
    function = {"Type": "AWS::Lambda::Function", "Properties": fn_props}
    return {role_id: role, logical_id: function}


def _expand_simple_table(props: dict) -> dict:
    pk = props.get("PrimaryKey", {"Name": "id", "Type": "String"})
    table_props = {
        "AttributeDefinitions": [{"AttributeName": pk["Name"], "AttributeType": _DDB_TYPE[pk["Type"]]}],
        "KeySchema": [{"AttributeName": pk["Name"], "KeyType": "HASH"}],
        "BillingMode": "PAY_PER_REQUEST",
    }
    if "TableName" in props:
        table_props["TableName"] = props["TableName"]
    return {"Type": "AWS::DynamoDB::Table", "Properties": table_props}
