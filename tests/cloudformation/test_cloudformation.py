"""Tests for the local CloudFormation server.

The unit tests (template parsing, intrinsics, ordering) need nothing running.
The deploy lifecycle test provisions into the real engines and is skipped unless
S3Proxy and DynamoDB Local are up:
    docker compose up -d s3proxy dynamodb
"""

import json

import boto3
import pytest

from oblako.engines.cloudformation import create_app, start_in_thread
from oblako.engines.cloudformation.engine import (
    StackStore,
    _ordered,
    _resolve,
    parse_template,
)
from oblako.engines.cloudformation.transform import is_sam, transform_sam
from oblako.services import DynamoDBService, S3ProxyService

CREDS = dict(
    region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
)


def test_parse_yaml_short_tags():
    t = parse_template(
        "Resources:\n"
        "  B:\n"
        "    Type: AWS::S3::Bucket\n"
        "    Properties:\n"
        "      BucketName: !Sub '${AWS::StackName}-data'\n"
        "Outputs:\n"
        "  Arn:\n"
        "    Value: !GetAtt B.Arn\n"
    )
    assert t["Resources"]["B"]["Properties"]["BucketName"] == {
        "Fn::Sub": "${AWS::StackName}-data"
    }
    assert t["Outputs"]["Arn"]["Value"] == {"Fn::GetAtt": ["B", "Arn"]}


def test_parse_json():
    body = json.dumps({"Resources": {"B": {"Type": "AWS::S3::Bucket"}}})
    assert parse_template(body)["Resources"]["B"]["Type"] == "AWS::S3::Bucket"


def test_resolve_intrinsics():
    ctx = {"stack": "demo", "params": {"Env": "prod"}, "physical": {"B": "demo-bucket"}}
    assert _resolve({"Ref": "Env"}, ctx) == "prod"
    assert _resolve({"Ref": "AWS::Region"}, ctx) == "us-east-1"
    assert _resolve({"Ref": "AWS::StackName"}, ctx) == "demo"
    assert _resolve({"Ref": "B"}, ctx) == "demo-bucket"
    assert _resolve({"Fn::GetAtt": ["B", "Arn"]}, ctx) == "demo-bucket"
    assert _resolve({"Fn::Sub": "${Env}-${B}"}, ctx) == "prod-demo-bucket"
    assert _resolve({"Fn::Join": ["-", ["a", {"Ref": "Env"}, "z"]]}, ctx) == "a-prod-z"


def test_ordering_respects_dependson_and_refs():
    resources = {
        "Table": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {"Name": {"Ref": "Bucket"}},
        },
        "Bucket": {"Type": "AWS::S3::Bucket"},
        "Cluster": {"Type": "AWS::Redshift::Cluster", "DependsOn": "Table"},
    }
    order = _ordered(resources)
    assert order.index("Bucket") < order.index("Table")  # Ref dependency
    assert order.index("Table") < order.index("Cluster")  # DependsOn


def test_sam_transform_expands_function_and_table():
    template = parse_template(
        "Transform: AWS::Serverless-2016-10-31\n"
        "Resources:\n"
        "  Fn:\n"
        "    Type: AWS::Serverless::Function\n"
        "    Properties:\n"
        "      Handler: app.handler\n"
        "      Runtime: python3.12\n"
        "  Store:\n"
        "    Type: AWS::Serverless::SimpleTable\n"
        "    Properties:\n"
        "      PrimaryKey: {Name: pk, Type: String}\n"
    )
    assert is_sam(template)
    out = transform_sam(template)["Resources"]
    assert out["FnRole"]["Type"] == "AWS::IAM::Role"
    assert out["Fn"]["Type"] == "AWS::Lambda::Function"
    assert out["Fn"]["Properties"]["Role"] == {"Fn::GetAtt": ["FnRole", "Arn"]}
    assert out["Store"]["Type"] == "AWS::DynamoDB::Table"
    assert out["Store"]["Properties"]["KeySchema"] == [
        {"AttributeName": "pk", "KeyType": "HASH"}
    ]
    assert out["Store"]["Properties"]["AttributeDefinitions"] == [
        {"AttributeName": "pk", "AttributeType": "S"}
    ]
    assert "Transform" not in transform_sam(template)


def test_not_sam_without_transform():
    assert not is_sam({"Resources": {}})


def test_function_api_event_adds_implicit_rest_api():
    template = parse_template(
        "Transform: AWS::Serverless-2016-10-31\n"
        "Resources:\n"
        "  Fn:\n"
        "    Type: AWS::Serverless::Function\n"
        "    Properties:\n"
        "      Handler: app.handler\n"
        "      Runtime: python3.12\n"
        "      Events:\n"
        "        Get: {Type: Api, Properties: {Path: /x, Method: get}}\n"
    )
    out = transform_sam(template)["Resources"]
    assert out["ServerlessRestApi"]["Type"] == "AWS::ApiGateway::RestApi"
    # no implicit API when the function has no Api event
    no_event = parse_template(
        "Transform: AWS::Serverless-2016-10-31\n"
        "Resources:\n"
        "  Fn:\n"
        "    Type: AWS::Serverless::Function\n"
        "    Properties: {Handler: app.handler, Runtime: python3.12}\n"
    )
    assert "ServerlessRestApi" not in transform_sam(no_event)["Resources"]


def test_missing_stack_raises_validation_error():
    store = StackStore()
    client = boto3.client("cloudformation", endpoint_url=_serve(store), **CREDS)
    with pytest.raises(client.exceptions.ClientError) as exc:
        client.describe_stacks(StackName="nope")
    assert exc.value.response["Error"]["Code"] == "ValidationError"


def test_change_set_emits_stack_level_event():
    # sam deploy reads describe_stack_events[0] right after CreateChangeSet
    store = StackStore()
    store.create_change_set("s", '{"Resources": {}}', {}, "cs", "CREATE")
    events = store.describe_stack_events("s")
    assert events and events[0]["ResourceStatus"] == "REVIEW_IN_PROGRESS"
    assert events[0]["ResourceType"] == "AWS::CloudFormation::Stack"


def test_change_set_diffs_add_modify_remove():
    # A second change set against an existing stack must be a real diff, not a
    # blanket "Add" of every resource (which would re-create live resources).
    store = StackStore()
    bucket_a = {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": "a"}}
    bucket_b = {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": "b"}}

    t1 = json.dumps({"Resources": {"A": bucket_a, "B": bucket_b}})
    store.create_change_set("s", t1, {}, "cs1", "CREATE")
    cs1 = store.describe_change_set("s", "cs1")
    assert {(c["LogicalResourceId"], c["Action"]) for c in cs1["Changes"]} == {
        ("A", "Add"),
        ("B", "Add"),
    }

    # A unchanged, B modified, C added. (B's props differ from t1.)
    t2 = json.dumps(
        {
            "Resources": {
                "A": bucket_a,
                "B": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": "b2"}},
                "C": {"Type": "AWS::DynamoDB::Table"},
            }
        }
    )
    store.create_change_set("s", t2, {}, "cs2", "UPDATE")
    cs2 = store.describe_change_set("s", "cs2")
    # Unchanged A is omitted; only the modify + add show up.
    assert {(c["LogicalResourceId"], c["Action"]) for c in cs2["Changes"]} == {
        ("B", "Modify"),
        ("C", "Add"),
    }

    # Now drop A; B and C identical to t2 (so they're unchanged → omitted).
    t3 = json.dumps(
        {
            "Resources": {
                "B": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": "b2"}},
                "C": {"Type": "AWS::DynamoDB::Table"},
            }
        }
    )
    store.create_change_set("s", t3, {}, "cs3", "UPDATE")
    cs3 = store.describe_change_set("s", "cs3")
    assert {(c["LogicalResourceId"], c["Action"]) for c in cs3["Changes"]} == {
        ("A", "Remove"),
    }


def test_template_url_parsing(monkeypatch):
    import oblako.services as svc
    from oblako.engines.cloudformation.app import _fetch_template_url

    captured = {}

    class _Client:
        def get_object(self, Bucket, Key):
            captured["ref"] = (Bucket, Key)
            return {
                "Body": type("B", (), {"read": lambda self: b'{"Resources": {}}'})()
            }

    monkeypatch.setattr(
        svc,
        "S3ProxyService",
        lambda: type("S", (), {"get_client": lambda self: _Client()})(),
    )
    _fetch_template_url(
        "http://localhost:9099/my-bucket/abc.template"
    )  # path style (S3Proxy)
    assert captured["ref"] == ("my-bucket", "abc.template")
    _fetch_template_url(
        "https://my-bucket.s3.us-east-1.amazonaws.com/abc.template"
    )  # virtual-host
    assert captured["ref"] == ("my-bucket", "abc.template")


_PORT = 5611


def _serve(store=None):
    """Start an isolated CFN server (own StackStore) for a test; return its URL."""
    global _PORT
    _PORT += 1
    import uvicorn

    app = create_app(store)
    config = uvicorn.Config(app, host="127.0.0.1", port=_PORT, log_level="warning")
    server = uvicorn.Server(config)
    import threading
    import time

    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        from oblako.engines.cloudformation import is_running

        if is_running(_PORT):
            break
        time.sleep(0.05)
    return f"http://localhost:{_PORT}"


def _engines_up() -> bool:
    try:
        S3ProxyService().get_client().list_buckets()
        DynamoDBService(host_port=8001).get_client().list_tables()
        return True
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _engines_up(), reason="S3Proxy + DynamoDB Local not running")
def test_deploy_lifecycle_provisions_real_engines():
    cfn = boto3.client("cloudformation", endpoint_url=start_in_thread(), **CREDS)
    s3 = S3ProxyService().get_client()
    ddb = DynamoDBService(host_port=8001).get_client()

    template = json.dumps(
        {
            "Parameters": {"Env": {"Type": "String", "Default": "test"}},
            "Resources": {
                "Data": {
                    "Type": "AWS::S3::Bucket",
                    "Properties": {"BucketName": {"Fn::Sub": "oblako-cfntest-${Env}"}},
                },
                "Items": {
                    "Type": "AWS::DynamoDB::Table",
                    "DependsOn": "Data",
                    "Properties": {
                        "TableName": {"Fn::Sub": "${Env}-cfntest-items"},
                        "AttributeDefinitions": [
                            {"AttributeName": "id", "AttributeType": "S"}
                        ],
                        "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
                    },
                },
            },
            "Outputs": {
                "Bucket": {"Value": {"Ref": "Data"}},
                "Table": {"Value": {"Ref": "Items"}},
            },
        }
    )

    cfn.create_change_set(
        StackName="cfntest",
        TemplateBody=template,
        ChangeSetName="cs",
        ChangeSetType="CREATE",
        Parameters=[{"ParameterKey": "Env", "ParameterValue": "test"}],
    )
    desc = cfn.describe_change_set(StackName="cfntest", ChangeSetName="cs")
    assert desc["Status"] == "CREATE_COMPLETE"
    assert {c["ResourceChange"]["ResourceType"] for c in desc["Changes"]} == {
        "AWS::S3::Bucket",
        "AWS::DynamoDB::Table",
    }

    cfn.execute_change_set(StackName="cfntest", ChangeSetName="cs")
    cfn.get_waiter("stack_create_complete").wait(StackName="cfntest")
    stack = cfn.describe_stacks(StackName="cfntest")["Stacks"][0]
    assert stack["StackStatus"] == "CREATE_COMPLETE"
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    assert outputs == {"Bucket": "oblako-cfntest-test", "Table": "test-cfntest-items"}

    try:
        assert "oblako-cfntest-test" in [
            b["Name"] for b in s3.list_buckets()["Buckets"]
        ]
        assert "test-cfntest-items" in ddb.list_tables()["TableNames"]
    finally:
        cfn.delete_stack(StackName="cfntest")
        cfn.get_waiter("stack_delete_complete").wait(StackName="cfntest")

    assert "oblako-cfntest-test" not in [
        b["Name"] for b in s3.list_buckets()["Buckets"]
    ]
    assert "test-cfntest-items" not in ddb.list_tables()["TableNames"]


@pytest.mark.integration
@pytest.mark.skipif(not _engines_up(), reason="S3Proxy + DynamoDB Local not running")
def test_update_change_set_adds_and_removes_real_resources():
    # Redeploying a changed template to an existing stack must apply a diff:
    # leave the unchanged bucket alone, drop the removed table, status UPDATE_*.
    cfn = boto3.client("cloudformation", endpoint_url=start_in_thread(), **CREDS)
    s3 = S3ProxyService().get_client()
    ddb = DynamoDBService(host_port=8001).get_client()

    v1 = json.dumps(
        {
            "Resources": {
                "Data": {
                    "Type": "AWS::S3::Bucket",
                    "Properties": {"BucketName": "oblako-cfnup"},
                },
                "Items": {
                    "Type": "AWS::DynamoDB::Table",
                    "Properties": {
                        "TableName": "cfnup-items",
                        "AttributeDefinitions": [
                            {"AttributeName": "id", "AttributeType": "S"}
                        ],
                        "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
                    },
                },
            }
        }
    )
    # v2 keeps the bucket verbatim and removes the table.
    v2 = json.dumps(
        {
            "Resources": {
                "Data": {
                    "Type": "AWS::S3::Bucket",
                    "Properties": {"BucketName": "oblako-cfnup"},
                },
            }
        }
    )

    cfn.create_change_set(
        StackName="cfnup", TemplateBody=v1, ChangeSetName="c1", ChangeSetType="CREATE"
    )
    cfn.execute_change_set(StackName="cfnup", ChangeSetName="c1")
    cfn.get_waiter("stack_create_complete").wait(StackName="cfnup")
    try:
        assert "oblako-cfnup" in [b["Name"] for b in s3.list_buckets()["Buckets"]]
        assert "cfnup-items" in ddb.list_tables()["TableNames"]

        cfn.create_change_set(
            StackName="cfnup",
            TemplateBody=v2,
            ChangeSetName="c2",
            ChangeSetType="UPDATE",
        )
        # The change set is a single Remove (the unchanged bucket is omitted).
        desc = cfn.describe_change_set(StackName="cfnup", ChangeSetName="c2")
        assert [
            (c["ResourceChange"]["LogicalResourceId"], c["ResourceChange"]["Action"])
            for c in desc["Changes"]
        ] == [("Items", "Remove")]

        cfn.execute_change_set(StackName="cfnup", ChangeSetName="c2")
        cfn.get_waiter("stack_update_complete").wait(StackName="cfnup")
        stack = cfn.describe_stacks(StackName="cfnup")["Stacks"][0]
        assert stack["StackStatus"] == "UPDATE_COMPLETE"
        # Table is gone; the untouched bucket survived the update.
        assert "cfnup-items" not in ddb.list_tables()["TableNames"]
        assert "oblako-cfnup" in [b["Name"] for b in s3.list_buckets()["Buckets"]]
    finally:
        cfn.delete_stack(StackName="cfnup")
        cfn.get_waiter("stack_delete_complete").wait(StackName="cfnup")


def _moto_client(service):
    return boto3.client(service, endpoint_url="http://localhost:5500", **CREDS)


def _moto_up() -> bool:
    try:
        _moto_client("lambda").list_functions()
        return _engines_up()
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _moto_up(), reason="moto + DynamoDB Local not running")
def test_sam_deploy_function_to_moto_and_table_to_dynamodb():
    cfn = boto3.client("cloudformation", endpoint_url=start_in_thread(), **CREDS)
    lam, iam = _moto_client("lambda"), _moto_client("iam")
    ddb = DynamoDBService(host_port=8001).get_client()

    sam = (
        "Transform: AWS::Serverless-2016-10-31\n"
        "Resources:\n"
        "  Worker:\n"
        "    Type: AWS::Serverless::Function\n"
        "    Properties:\n"
        "      FunctionName: cfntest-worker\n"
        "      Handler: app.handler\n"
        "      Runtime: python3.12\n"
        "  Store:\n"
        "    Type: AWS::Serverless::SimpleTable\n"
        "    Properties:\n"
        "      TableName: cfntest-sam-store\n"
    )
    cfn.create_change_set(
        StackName="cfnsam", TemplateBody=sam, ChangeSetName="cs", ChangeSetType="CREATE"
    )
    desc = cfn.describe_change_set(StackName="cfnsam", ChangeSetName="cs")
    # the change set shows the EXPANDED base CFN types
    assert {c["ResourceChange"]["ResourceType"] for c in desc["Changes"]} == {
        "AWS::IAM::Role",
        "AWS::Lambda::Function",
        "AWS::DynamoDB::Table",
    }
    cfn.execute_change_set(StackName="cfnsam", ChangeSetName="cs")
    cfn.get_waiter("stack_create_complete").wait(StackName="cfnsam")

    try:
        assert "cfntest-worker" in [
            f["FunctionName"] for f in lam.list_functions()["Functions"]
        ]
        assert "WorkerRole" in [r["RoleName"] for r in iam.list_roles()["Roles"]]
        assert (
            "cfntest-sam-store" in ddb.list_tables()["TableNames"]
        )  # real DynamoDB Local
    finally:
        cfn.delete_stack(StackName="cfnsam")
        cfn.get_waiter("stack_delete_complete").wait(StackName="cfnsam")

    assert "cfntest-worker" not in [
        f["FunctionName"] for f in lam.list_functions()["Functions"]
    ]
    assert "cfntest-sam-store" not in ddb.list_tables()["TableNames"]


def _sfn_up() -> bool:
    try:
        from oblako.services import StepFunctionsService

        StepFunctionsService().get_client().list_state_machines()
        return True
    except Exception:
        return False


def _opensearch_up() -> bool:
    try:
        import httpx

        return (
            httpx.get("http://localhost:9200/_cluster/health", timeout=3).status_code
            == 200
        )
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.skipif(
    not (_sfn_up() and _opensearch_up()),
    reason="Step Functions Local + OpenSearch not running",
)
def test_deploy_statemachine_and_opensearch_domain():
    import time

    from oblako.services import StepFunctionsService

    cfn = boto3.client("cloudformation", endpoint_url=start_in_thread(), **CREDS)
    sfn = StepFunctionsService().get_client()
    asl = json.dumps(
        {"StartAt": "Done", "States": {"Done": {"Type": "Pass", "End": True}}}
    )
    template = json.dumps(
        {
            "Resources": {
                "Flow": {
                    "Type": "AWS::StepFunctions::StateMachine",
                    "Properties": {
                        "StateMachineName": "cfntest-flow",
                        "DefinitionString": asl,
                        "RoleArn": "arn:aws:iam::012345678901:role/DummyRole",
                    },
                },
                "Search": {
                    "Type": "AWS::OpenSearchService::Domain",
                    "Properties": {"DomainName": "cfntest-search"},
                },
            },
            "Outputs": {
                "FlowName": {"Value": {"Fn::GetAtt": ["Flow", "Name"]}},
                "Endpoint": {"Value": {"Fn::GetAtt": ["Search", "DomainEndpoint"]}},
            },
        }
    )
    cfn.create_change_set(
        StackName="cfneng",
        TemplateBody=template,
        ChangeSetName="cs",
        ChangeSetType="CREATE",
    )
    cfn.execute_change_set(StackName="cfneng", ChangeSetName="cs")
    cfn.get_waiter("stack_create_complete").wait(StackName="cfneng")

    outs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName="cfneng")["Stacks"][0]["Outputs"]
    }
    assert outs["FlowName"] == "cfntest-flow"  # attribute-aware GetAtt
    assert outs["Endpoint"] == "localhost:9200"
    try:
        assert "cfntest-flow" in [
            m["name"] for m in sfn.list_state_machines()["stateMachines"]
        ]
    finally:
        cfn.delete_stack(StackName="cfneng")
        cfn.get_waiter("stack_delete_complete").wait(StackName="cfneng")

    # Step Functions Local deletes asynchronously — poll for the machine to vanish.
    for _ in range(20):
        if "cfntest-flow" not in [
            m["name"] for m in sfn.list_state_machines()["stateMachines"]
        ]:
            break
        time.sleep(0.5)
    assert "cfntest-flow" not in [
        m["name"] for m in sfn.list_state_machines()["stateMachines"]
    ]


@pytest.mark.integration
@pytest.mark.skipif(
    not (_moto_up() and _sfn_up()),
    reason="moto + DynamoDB Local + Step Functions not running",
)
def test_deploy_all_resource_types_one_stack():
    """One stack provisioning every supported type into its real engine at once.

    Covers the resource types whose engines run in the CI integration tier
    (S3Proxy, DynamoDB Local, Step Functions Local, moto). OpenSearch::Domain is
    exercised separately (it needs the OpenSearch container) in the test above.
    """
    from oblako.services import StepFunctionsService

    cfn = boto3.client("cloudformation", endpoint_url=start_in_thread(), **CREDS)
    s3 = S3ProxyService().get_client()
    ddb = DynamoDBService(host_port=8001).get_client()
    sfn = StepFunctionsService().get_client()
    lam, iam, rs, rds_, apigw = (
        _moto_client(s) for s in ("lambda", "iam", "redshift", "rds", "apigateway")
    )
    asl = json.dumps(
        {"StartAt": "Done", "States": {"Done": {"Type": "Pass", "End": True}}}
    )

    template = json.dumps(
        {
            "Transform": "AWS::Serverless-2016-10-31",
            "Resources": {
                "Bucket": {
                    "Type": "AWS::S3::Bucket",
                    "Properties": {"BucketName": "cfnall-bucket"},
                },
                "Table": {
                    "Type": "AWS::DynamoDB::Table",
                    "Properties": {
                        "TableName": "cfnall-table",
                        "AttributeDefinitions": [
                            {"AttributeName": "id", "AttributeType": "S"}
                        ],
                        "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
                    },
                },
                "Flow": {
                    "Type": "AWS::StepFunctions::StateMachine",
                    "Properties": {
                        "StateMachineName": "cfnall-flow",
                        "DefinitionString": asl,
                        "RoleArn": "arn:aws:iam::012345678901:role/DummyRole",
                    },
                },
                "Warehouse": {
                    "Type": "AWS::Redshift::Cluster",
                    "Properties": {
                        "ClusterIdentifier": "cfnall-dw",
                        "NodeType": "ra3.xlplus",
                        "MasterUsername": "oblako",
                        "MasterUserPassword": "Oblako123",
                    },
                },
                "Database": {
                    "Type": "AWS::RDS::DBInstance",
                    "Properties": {
                        "DBInstanceIdentifier": "cfnall-db",
                        "Engine": "postgres",
                        "MasterUsername": "oblako",
                        "MasterUserPassword": "Oblako123",
                    },
                },
                "Worker": {
                    "Type": "AWS::Serverless::Function",
                    "Properties": {
                        "FunctionName": "cfnall-fn",
                        "Handler": "app.handler",
                        "Runtime": "python3.12",
                        "Events": {
                            "Get": {
                                "Type": "Api",
                                "Properties": {"Path": "/x", "Method": "get"},
                            }
                        },
                    },
                },
            },
        }
    )
    cfn.create_change_set(
        StackName="cfnall",
        TemplateBody=template,
        ChangeSetName="cs",
        ChangeSetType="CREATE",
    )
    cfn.execute_change_set(StackName="cfnall", ChangeSetName="cs")
    cfn.get_waiter("stack_create_complete").wait(StackName="cfnall")

    try:
        assert "cfnall-bucket" in [b["Name"] for b in s3.list_buckets()["Buckets"]]
        assert "cfnall-table" in ddb.list_tables()["TableNames"]
        assert "cfnall-flow" in [
            m["name"] for m in sfn.list_state_machines()["stateMachines"]
        ]
        assert "cfnall-dw" in [
            c["ClusterIdentifier"] for c in rs.describe_clusters()["Clusters"]
        ]
        assert "cfnall-db" in [
            d["DBInstanceIdentifier"]
            for d in rds_.describe_db_instances()["DBInstances"]
        ]
        assert "cfnall-fn" in [
            f["FunctionName"] for f in lam.list_functions()["Functions"]
        ]
        assert any("Worker" in r["RoleName"] for r in iam.list_roles()["Roles"])
        assert "ServerlessRestApi" in [
            a["name"] for a in apigw.get_rest_apis()["items"]
        ]
    finally:
        cfn.delete_stack(StackName="cfnall")
        cfn.get_waiter("stack_delete_complete").wait(StackName="cfnall")

    assert "cfnall-bucket" not in [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert "cfnall-db" not in [
        d["DBInstanceIdentifier"] for d in rds_.describe_db_instances()["DBInstances"]
    ]
