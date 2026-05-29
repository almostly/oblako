"""CloudFormation resource providers.

Create/delete each resource type in oblako's REAL engines (not a mock). This is
what makes `aws cloudformation deploy` provision actual buckets/tables/clusters
in oblako.
"""

from __future__ import annotations

import json
import uuid


def _s3_client():
    from oblako.services import S3ProxyService

    return S3ProxyService().get_client()


def _dynamodb_client():
    from oblako.services import DynamoDBService

    return DynamoDBService(host_port=8001).get_client()


def _moto_client(service):
    from oblako.services import boto

    return boto.client(service, "http://localhost:5500", region="us-east-1")


# AWS::S3::Bucket
def _s3_create(logical_id, props, ctx):
    name = (
        props.get("BucketName")
        or f"{ctx['stack']}-{logical_id}-{uuid.uuid4().hex[:8]}".lower()
    )
    _s3_client().create_bucket(Bucket=name)
    return name


def _s3_delete(physical_id, props):
    s3 = _s3_client()
    try:
        objs = s3.list_objects_v2(Bucket=physical_id).get("Contents", [])
        for o in objs:
            s3.delete_object(Bucket=physical_id, Key=o["Key"])
        s3.delete_bucket(Bucket=physical_id)
    except Exception:  # noqa: BLE001
        pass


# AWS::DynamoDB::Table
def _ddb_create(logical_id, props, ctx):
    name = props.get("TableName") or f"{ctx['stack']}-{logical_id}"
    kwargs = {
        "TableName": name,
        "AttributeDefinitions": props["AttributeDefinitions"],
        "KeySchema": props["KeySchema"],
    }
    if "BillingMode" in props:
        kwargs["BillingMode"] = props["BillingMode"]
    if "ProvisionedThroughput" in props:
        kwargs["ProvisionedThroughput"] = props["ProvisionedThroughput"]
    elif "BillingMode" not in props:
        kwargs["BillingMode"] = "PAY_PER_REQUEST"
    _dynamodb_client().create_table(**kwargs)
    return name


def _ddb_delete(physical_id, props):
    try:
        _dynamodb_client().delete_table(TableName=physical_id)
    except Exception:  # noqa: BLE001
        pass


# AWS::Redshift::Cluster (control plane via moto)
def _redshift_create(logical_id, props, ctx):
    cid = props.get("ClusterIdentifier") or f"{ctx['stack']}-{logical_id}".lower()
    rs = _moto_client("redshift")
    kwargs = {
        "ClusterIdentifier": cid,
        "NodeType": props.get("NodeType", "ra3.xlplus"),
        "MasterUsername": props.get("MasterUsername", "oblako"),
        "MasterUserPassword": props.get("MasterUserPassword", "Oblako123"),
    }
    if "DBName" in props:
        kwargs["DBName"] = props["DBName"]
    if "NumberOfNodes" in props:
        kwargs["NumberOfNodes"] = int(props["NumberOfNodes"])
        kwargs["ClusterType"] = "multi-node"
    rs.create_cluster(**kwargs)
    return cid


def _redshift_delete(physical_id, props):
    try:
        _moto_client("redshift").delete_cluster(
            ClusterIdentifier=physical_id, SkipFinalClusterSnapshot=True
        )
    except Exception:  # noqa: BLE001
        pass


# AWS::RDS::DBInstance (control plane via moto)
def _rds_create(logical_id, props, ctx):
    iid = props.get("DBInstanceIdentifier") or f"{ctx['stack']}-{logical_id}".lower()
    kwargs = {
        "DBInstanceIdentifier": iid,
        "Engine": props.get("Engine", "postgres"),
        "DBInstanceClass": props.get("DBInstanceClass", "db.t3.micro"),
        "MasterUsername": props.get("MasterUsername", "oblako"),
        "MasterUserPassword": props.get("MasterUserPassword", "Oblako123"),
        "AllocatedStorage": int(props.get("AllocatedStorage", 20)),
    }
    if "DBName" in props:
        kwargs["DBName"] = props["DBName"]
    _moto_client("rds").create_db_instance(**kwargs)
    return iid


def _rds_delete(physical_id, props):
    try:
        _moto_client("rds").delete_db_instance(
            DBInstanceIdentifier=physical_id, SkipFinalSnapshot=True
        )
    except Exception:  # noqa: BLE001
        pass


# AWS::IAM::Role (control plane via moto) — e.g. the implicit role SAM creates
def _iam_create(logical_id, props, ctx):
    name = props.get("RoleName") or f"{ctx['stack']}-{logical_id}"
    trust = props.get("AssumeRolePolicyDocument", {})
    iam = _moto_client("iam")
    resp = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))
    return resp["Role"]["Arn"]  # physical id is the ARN, so GetAtt .Arn resolves


def _iam_delete(physical_id, props):
    name = physical_id.rsplit("/", 1)[-1]
    try:
        _moto_client("iam").delete_role(RoleName=name)
    except Exception:  # noqa: BLE001
        pass


# AWS::EC2::Instance — moto metadata + a real container-backed instance
# (instance == container, EBS == Docker volume), same as Ec2Service.run_instance.
def _ec2_create(logical_id, props, ctx):
    from oblako.services.ec2 import start_instance_container

    ec2 = _moto_client("ec2")
    image_id = props.get("ImageId", "ami-0abcdef1234567890")
    iid = ec2.run_instances(
        ImageId=image_id, InstanceType=props.get("InstanceType", "t3.micro"),
        MinCount=1, MaxCount=1,
    )["Instances"][0]["InstanceId"]
    tags = props.get("Tags")
    if tags:
        ec2.create_tags(Resources=[iid], Tags=tags)
    # back it with a real container; `Image`/`Ports` are oblako extensions letting
    # a resource pick its backing image + publish ports (e.g. a notebook instance).
    start_instance_container(iid, image_id=image_id, image=props.get("Image"),
                             published_ports=props.get("Ports"))
    return iid  # physical id is the InstanceId


def _ec2_delete(physical_id, props):
    from oblako.services.ec2 import terminate_instance_container

    try:
        _moto_client("ec2").terminate_instances(InstanceIds=[physical_id])
    except Exception:  # noqa: BLE001
        pass
    terminate_instance_container(physical_id)


# AWS::Lambda::Function (control plane via moto). oblako has no Lambda engine —
# real execution stays in `sam local`; this stores a describable record (a
# placeholder zip; the original CodeUri is kept in the description).
def _lambda_create(logical_id, props, ctx):
    name = props.get("FunctionName") or f"{ctx['stack']}-{logical_id}"
    kwargs = {
        "FunctionName": name,
        "Runtime": props.get("Runtime", "python3.12"),
        "Role": props["Role"],
        "Handler": props.get("Handler", "app.handler"),
        "Code": {"ZipFile": b"oblako-placeholder"},
        "PackageType": "Zip",
        "Description": f"oblako: CodeUri={props.get('CodeUri', '')} (run via sam local)",
    }
    if "Timeout" in props:
        kwargs["Timeout"] = int(props["Timeout"])
    if "MemorySize" in props:
        kwargs["MemorySize"] = int(props["MemorySize"])
    if "Environment" in props:
        kwargs["Environment"] = props["Environment"]
    _moto_client("lambda").create_function(**kwargs)
    return name


def _lambda_delete(physical_id, props):
    try:
        _moto_client("lambda").delete_function(FunctionName=physical_id)
    except Exception:  # noqa: BLE001
        pass


# AWS::ApiGateway::RestApi (control plane via moto)
def _apigw_create(logical_id, props, ctx):
    name = props.get("Name") or f"{ctx['stack']}-{logical_id}"
    return _moto_client("apigateway").create_rest_api(name=name)["id"]


def _apigw_delete(physical_id, props):
    try:
        _moto_client("apigateway").delete_rest_api(restApiId=physical_id)
    except Exception:  # noqa: BLE001
        pass


# AWS::StepFunctions::StateMachine -> Step Functions Local (real engine)
def _sfn_create(logical_id, props, ctx):
    from oblako.services import StepFunctionsService

    name = props.get("StateMachineName") or f"{ctx['stack']}-{logical_id}"
    definition = props.get("DefinitionString")
    if definition is None and "Definition" in props:
        definition = json.dumps(props["Definition"])
    kwargs = {
        "name": name,
        "definition": definition,
        "roleArn": props.get("RoleArn", "arn:aws:iam::012345678901:role/DummyRole"),
    }
    if "StateMachineType" in props:
        kwargs["type"] = props["StateMachineType"]
    arn = (
        StepFunctionsService()
        .get_client()
        .create_state_machine(**kwargs)["stateMachineArn"]
    )
    # Ref returns the ARN (as in real CFN); GetAtt Name returns the name.
    return {"PhysicalId": arn, "Attributes": {"Name": name, "Arn": arn}}


def _sfn_delete(physical_id, props):
    from oblako.services import StepFunctionsService

    try:
        StepFunctionsService().get_client().delete_state_machine(
            stateMachineArn=physical_id
        )
    except Exception:  # noqa: BLE001
        pass


# AWS::OpenSearchService::Domain -> the shared local OpenSearch engine.
# oblako runs a single OpenSearch; a "domain" is a handle to it (simulated
# topology, real engine). We verify it's reachable and hand back its endpoint;
# we don't spin up — or, on delete, tear down — the shared cluster.
def _opensearch_create(logical_id, props, ctx):
    import httpx

    from oblako.services import OpenSearchService

    svc = OpenSearchService()
    name = (props.get("DomainName") or f"{ctx['stack']}-{logical_id}").lower()
    httpx.get(f"{svc.url}/_cluster/health", timeout=5.0).raise_for_status()
    endpoint = svc.url.split("://", 1)[-1]
    return {
        "PhysicalId": name,
        "Attributes": {
            "DomainEndpoint": endpoint,
            "Arn": f"arn:aws:es:us-east-1:000000000000:domain/{name}",
        },
    }


def _opensearch_delete(physical_id, props):
    pass  # shared engine — the domain is just a handle


# resource_type -> (create, delete)
PROVIDERS = {
    "AWS::S3::Bucket": (_s3_create, _s3_delete),
    "AWS::DynamoDB::Table": (_ddb_create, _ddb_delete),
    "AWS::Redshift::Cluster": (_redshift_create, _redshift_delete),
    "AWS::RDS::DBInstance": (_rds_create, _rds_delete),
    "AWS::IAM::Role": (_iam_create, _iam_delete),
    "AWS::EC2::Instance": (_ec2_create, _ec2_delete),
    "AWS::Lambda::Function": (_lambda_create, _lambda_delete),
    "AWS::ApiGateway::RestApi": (_apigw_create, _apigw_delete),
    "AWS::StepFunctions::StateMachine": (_sfn_create, _sfn_delete),
    "AWS::OpenSearchService::Domain": (_opensearch_create, _opensearch_delete),
}
