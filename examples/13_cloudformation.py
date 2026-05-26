"""Example 13: local CloudFormation that provisions oblako's real resources.

A boto3 'cloudformation' client deploys a stack, and the resources land in
oblako's actual engines — the S3::Bucket in S3Proxy, the DynamoDB::Table in
DynamoDB Local. Supported types: S3::Bucket, DynamoDB::Table, Redshift::Cluster
(via moto), RDS::DBInstance (via moto).

This is the same wire protocol the AWS CLI and SAM speak, so the CLI works too:

    oblako cloudformation                                   # start the server (:5601)
    export AWS_ENDPOINT_URL_CLOUDFORMATION=http://localhost:5601
    aws cloudformation deploy --template-file t.yaml --stack-name demo
    sam deploy ...                                          # for plain CFN resources

Prerequisites:
    make up           # S3Proxy (:9000) + DynamoDB Local (:8001)
"""

import json

from oblako_ml.services import CloudFormationService, DynamoDBService, S3ProxyService

cfn = CloudFormationService().get_client()  # auto-starts the in-process server

TEMPLATE = json.dumps({
    "AWSTemplateFormatVersion": "2010-09-09",
    "Parameters": {"Env": {"Type": "String", "Default": "demo"}},
    "Resources": {
        "ModelStore": {
            "Type": "AWS::S3::Bucket",
            "Properties": {"BucketName": {"Fn::Sub": "oblako-${Env}-models"}},
        },
        "Predictions": {
            "Type": "AWS::DynamoDB::Table",
            "DependsOn": "ModelStore",
            "Properties": {
                "TableName": {"Fn::Sub": "${Env}-predictions"},
                "AttributeDefinitions": [{"AttributeName": "id", "AttributeType": "S"}],
                "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
            },
        },
    },
    "Outputs": {
        "Bucket": {"Value": {"Ref": "ModelStore"}},
        "Table": {"Value": {"Ref": "Predictions"}},
    },
})

STACK = "ml-pipeline"


def deploy(stack, template, params=None):
    """Mirror `aws cloudformation deploy`: change set -> execute -> wait."""
    cfn.create_change_set(
        StackName=stack, TemplateBody=template, ChangeSetName="deploy", ChangeSetType="CREATE",
        Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in (params or {}).items()],
    )
    desc = cfn.describe_change_set(StackName=stack, ChangeSetName="deploy")
    print(f"Change set: {desc['Status']}")
    for c in desc["Changes"]:
        rc = c["ResourceChange"]
        print(f"  {rc['Action']:>6}  {rc['LogicalResourceId']:<12} {rc['ResourceType']}")
    cfn.execute_change_set(StackName=stack, ChangeSetName="deploy")
    cfn.get_waiter("stack_create_complete").wait(StackName=stack)


deploy(STACK, TEMPLATE)

stack = cfn.describe_stacks(StackName=STACK)["Stacks"][0]
print(f"\nStack {stack['StackName']}: {stack['StackStatus']}")
outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
print(f"Outputs: {outputs}")

# The resources are real — confirm them through oblako's own clients.
s3 = S3ProxyService().get_client()
ddb = DynamoDBService(host_port=8001).get_client()
print(f"\nIn S3Proxy:        {outputs['Bucket'] in [b['Name'] for b in s3.list_buckets()['Buckets']]}")
print(f"In DynamoDB Local: {outputs['Table'] in ddb.list_tables()['TableNames']}")

print("\nStack events:")
for e in reversed(cfn.describe_stack_events(StackName=STACK)["StackEvents"]):
    print(f"  {e['ResourceStatus']:<16} {e['LogicalResourceId']} ({e['ResourceType']})")

print(f"\nTear down with: cfn.delete_stack(StackName='{STACK}')  # removes the bucket + table")
