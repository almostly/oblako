"""Integration test: Redshift Serverless namespaces and workgroups.

Requires the Redshift engine on 5439. The redshift-control proxy, the Data API and
the CloudFormation engine start in-process on free ports, so the test runs the
code under change. A namespace creates its admin user and database in the engine,
a workgroup's endpoint is the engine, and deleting the namespace drops both.
"""

import json

import boto3
import psycopg
import pytest

from oblako.engines import cloudformation, redshift_control, redshift_data
from oblako.engines.cloudformation.engine import StackStore
from tests.ports import free_port

CREDS = dict(
    region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
)
NAMESPACE = "pytest-ns"
WORKGROUP = "pytest-wg"
ADMIN, PASSWORD, DATABASE = "pytest_admin", "Secret123x", "pytest_dev"


@pytest.fixture(scope="module")
def serverless():
    url = redshift_control.start_in_thread(free_port())
    client = boto3.client("redshift-serverless", endpoint_url=url, **CREDS)

    def cleanup():
        for call, key, name in (
            (client.delete_workgroup, "workgroupName", WORKGROUP),
            (client.delete_namespace, "namespaceName", NAMESPACE),
        ):
            try:
                call(**{key: name})
            except client.exceptions.ResourceNotFoundException:
                pass

    cleanup()
    yield client
    cleanup()


@pytest.fixture
def stack(serverless):
    serverless.create_namespace(
        namespaceName=NAMESPACE,
        adminUsername=ADMIN,
        adminUserPassword=PASSWORD,
        dbName=DATABASE,
    )
    serverless.create_workgroup(workgroupName=WORKGROUP, namespaceName=NAMESPACE)
    yield serverless
    serverless.delete_workgroup(workgroupName=WORKGROUP)
    serverless.delete_namespace(namespaceName=NAMESPACE)


def _connect(endpoint, user=ADMIN, password=PASSWORD, dbname=DATABASE):
    return psycopg.connect(
        host=endpoint["address"],
        port=endpoint["port"],
        user=user,
        password=password,
        dbname=dbname,
        sslmode="require",
        connect_timeout=5,
    )


def test_workgroup_endpoint_takes_the_namespace_admin(stack):
    workgroup = stack.get_workgroup(workgroupName=WORKGROUP)["workgroup"]
    assert workgroup["status"] == "AVAILABLE"
    assert workgroup["namespaceName"] == NAMESPACE
    assert workgroup["workgroupArn"].startswith(
        "arn:aws:redshift-serverless:us-east-1:"
    )
    with _connect(workgroup["endpoint"]) as conn:
        row = conn.execute("SELECT current_user, current_database()").fetchone()
    assert row == (ADMIN, DATABASE)
    with pytest.raises(psycopg.OperationalError):
        _connect(workgroup["endpoint"], password="wrong-password")


def test_namespace_and_workgroup_are_listed(stack):
    namespace = stack.get_namespace(namespaceName=NAMESPACE)["namespace"]
    assert namespace["adminUsername"] == ADMIN and namespace["dbName"] == DATABASE
    names = [w["workgroupName"] for w in stack.list_workgroups()["workgroups"]]
    assert WORKGROUP in names


def test_errors_match_aws(stack):
    with pytest.raises(stack.exceptions.ConflictException):
        stack.create_workgroup(workgroupName=WORKGROUP, namespaceName=NAMESPACE)
    # a namespace in use by a workgroup cannot be deleted
    with pytest.raises(stack.exceptions.ConflictException):
        stack.delete_namespace(namespaceName=NAMESPACE)
    with pytest.raises(stack.exceptions.ResourceNotFoundException):
        stack.get_workgroup(workgroupName="no-such-workgroup")
    with pytest.raises(stack.exceptions.ValidationException):
        stack.create_namespace(namespaceName="Bad_Name")


def test_deleting_the_namespace_drops_its_user_and_database(serverless):
    serverless.create_namespace(
        namespaceName=NAMESPACE,
        adminUsername=ADMIN,
        adminUserPassword=PASSWORD,
        dbName=DATABASE,
    )
    serverless.delete_namespace(namespaceName=NAMESPACE)
    with psycopg.connect(
        host="localhost",
        port=5439,
        user="oblako",
        password="oblako",
        dbname="oblako",
        sslmode="require",
    ) as conn:
        roles = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (ADMIN,)
        ).fetchall()
        databases = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)
        ).fetchall()
    assert roles == [] and databases == []


def test_data_api_runs_on_a_workgroup(stack):
    url = redshift_data.start_in_thread(free_port())
    data = boto3.client("redshift-data", endpoint_url=url, **CREDS)
    out = data.execute_statement(
        WorkgroupName=WORKGROUP, Database=DATABASE, Sql="SELECT 41 + 1"
    )
    assert out["WorkgroupName"] == WORKGROUP and "ClusterIdentifier" not in out
    result = data.get_statement_result(Id=out["Id"])
    assert result["Records"] == [[{"longValue": 42}]]
    with pytest.raises(data.exceptions.ValidationException):
        data.execute_statement(
            WorkgroupName="no-such-workgroup", Database=DATABASE, Sql="SELECT 1"
        )


def test_cloudformation_creates_and_deletes_a_workgroup(serverless):
    url = cloudformation.start_in_thread(free_port(), StackStore())
    cfn = boto3.client("cloudformation", endpoint_url=url, **CREDS)
    template = {
        "Parameters": {"Password": {"Type": "String", "NoEcho": True}},
        "Resources": {
            "Namespace": {
                "Type": "AWS::RedshiftServerless::Namespace",
                "Properties": {
                    "NamespaceName": NAMESPACE,
                    "AdminUsername": ADMIN,
                    "AdminUserPassword": {"Ref": "Password"},
                    "DbName": DATABASE,
                },
            },
            "Workgroup": {
                "Type": "AWS::RedshiftServerless::Workgroup",
                "Properties": {
                    "WorkgroupName": WORKGROUP,
                    "NamespaceName": {"Ref": "Namespace"},
                    "PubliclyAccessible": True,
                },
            },
        },
        "Outputs": {
            "Host": {"Value": {"Fn::GetAtt": "Workgroup.Workgroup.Endpoint.Address"}},
            "Port": {"Value": {"Fn::GetAtt": "Workgroup.Workgroup.Endpoint.Port"}},
        },
    }
    cfn.create_stack(
        StackName="pytest-serverless",
        TemplateBody=json.dumps(template),
        Parameters=[{"ParameterKey": "Password", "ParameterValue": PASSWORD}],
    )
    cfn.get_waiter("stack_create_complete").wait(StackName="pytest-serverless")
    stack = cfn.describe_stacks(StackName="pytest-serverless")["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    assert outputs == {"Host": "localhost", "Port": "5439"}
    endpoint = {"address": outputs["Host"], "port": int(outputs["Port"])}
    with _connect(endpoint) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)

    cfn.delete_stack(StackName="pytest-serverless")
    cfn.get_waiter("stack_delete_complete").wait(StackName="pytest-serverless")
    names = [w["workgroupName"] for w in serverless.list_workgroups()["workgroups"]]
    assert WORKGROUP not in names
    with pytest.raises(serverless.exceptions.ResourceNotFoundException):
        serverless.get_namespace(namespaceName=NAMESPACE)
