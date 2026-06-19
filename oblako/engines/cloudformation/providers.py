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
        ImageId=image_id,
        InstanceType=props.get("InstanceType", "t3.micro"),
        MinCount=1,
        MaxCount=1,
    )["Instances"][0]["InstanceId"]
    tags = props.get("Tags")
    if tags:
        ec2.create_tags(Resources=[iid], Tags=tags)
    # back it with a real container; `Image`/`Ports` are oblako extensions letting
    # a resource pick its backing image + publish ports (e.g. a notebook instance).
    start_instance_container(
        iid,
        image_id=image_id,
        image=props.get("Image"),
        published_ports=props.get("Ports"),
    )
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


# ECS + ELBv2: a real container per task, a real Caddy proxy per load balancer.
# One shared EcsService/Elbv2Service across a deploy so the LB -> target-group ->
# listener -> service wiring (kept in-memory) is visible to every provider call.
_ECS = None
_ELBV2 = None


def _ecs_elbv2():
    global _ECS, _ELBV2
    if _ECS is None:
        from oblako.services import EcsService, Elbv2Service, MotoService

        moto = MotoService()
        _ELBV2 = Elbv2Service(moto=moto)
        _ECS = EcsService(moto=moto, elbv2=_ELBV2)
    return _ECS, _ELBV2


# AWS::ECS::Cluster (control plane via moto)
def _ecs_cluster_create(logical_id, props, ctx):
    name = props.get("ClusterName") or f"{ctx['stack']}-{logical_id}"
    _moto_client("ecs").create_cluster(clusterName=name)
    return name


def _ecs_cluster_delete(physical_id, props):
    try:
        _moto_client("ecs").delete_cluster(cluster=physical_id)
    except Exception:  # noqa: BLE001
        pass


def _lower_first(key):
    return key[:1].lower() + key[1:]


def _cfn_taskdef_to_boto(props):
    """Translate a CFN AWS::ECS::TaskDefinition (PascalCase) to boto3 (camelCase)."""
    out = {"family": props["Family"]}
    if "RequiresCompatibilities" in props:
        out["requiresCompatibilities"] = props["RequiresCompatibilities"]
    if "NetworkMode" in props:
        out["networkMode"] = props["NetworkMode"]
    if "Cpu" in props:
        out["cpu"] = str(props["Cpu"])
    if "Memory" in props:
        out["memory"] = str(props["Memory"])
    if "ExecutionRoleArn" in props:
        out["executionRoleArn"] = props["ExecutionRoleArn"]
    if "RuntimePlatform" in props:
        out["runtimePlatform"] = {
            _lower_first(k): v for k, v in props["RuntimePlatform"].items()
        }
    containers = []
    for c in props.get("ContainerDefinitions", []):
        cd = {"name": c["Name"], "image": c["Image"]}
        if "PortMappings" in c:
            cd["portMappings"] = [
                {
                    "containerPort": int(pm["ContainerPort"]),
                    "protocol": pm.get("Protocol", "tcp"),
                }
                for pm in c["PortMappings"]
            ]
        if "Environment" in c:
            cd["environment"] = [
                {"name": e["Name"], "value": e["Value"]} for e in c["Environment"]
            ]
        if "Command" in c:
            cd["command"] = c["Command"]
        # LogConfiguration (awslogs) is dropped: local containers log to the backend.
        containers.append(cd)
    out["containerDefinitions"] = containers
    return out


# AWS::ECS::TaskDefinition (moto metadata; the spec oblako runs from)
def _ecs_taskdef_create(logical_id, props, ctx):
    ecs, _ = _ecs_elbv2()
    return ecs.register_task_definition(**_cfn_taskdef_to_boto(props))  # Ref -> arn


def _ecs_taskdef_delete(physical_id, props):
    try:
        _moto_client("ecs").deregister_task_definition(taskDefinition=physical_id)
    except Exception:  # noqa: BLE001
        pass


# AWS::ECS::Service -> run desiredCount real tasks, register them as ALB targets
def _ecs_service_create(logical_id, props, ctx):
    ecs, _ = _ecs_elbv2()
    name = props.get("ServiceName") or f"{ctx['stack']}-{logical_id}"
    cluster = props.get("Cluster", "default")
    load_balancers = [
        {
            "targetGroupArn": lb["TargetGroupArn"],
            "containerName": lb["ContainerName"],
            "containerPort": int(lb["ContainerPort"]),
        }
        for lb in props.get("LoadBalancers", [])
    ]
    ecs.create_service(
        service_name=name,
        task_definition=props["TaskDefinition"],
        cluster=cluster,
        desired_count=int(props.get("DesiredCount", 1)),
        launch_type=props.get("LaunchType", "FARGATE"),
        load_balancers=load_balancers,
        network_configuration=props.get("NetworkConfiguration"),
    )
    # Ref returns the service ARN (as in real CFN); GetAtt Name/ServiceArn too.
    try:
        svc = ecs.get_client().describe_services(cluster=cluster, services=[name])[
            "services"
        ]
        arn = svc[0]["serviceArn"] if svc else name
    except Exception:  # noqa: BLE001
        arn = name
    return {"PhysicalId": arn, "Attributes": {"Name": name, "ServiceArn": arn}}


def _ecs_service_delete(physical_id, props):
    ecs, _ = _ecs_elbv2()
    name = physical_id.rsplit("/", 1)[-1]  # physical id may be the service ARN
    ecs.delete_service(name, cluster=props.get("Cluster", "default"))


# AWS::ElasticLoadBalancingV2::LoadBalancer -> a real Caddy reverse proxy
def _elb_lb_create(logical_id, props, ctx):
    _, elbv2 = _ecs_elbv2()
    name = props.get("Name") or f"{ctx['stack']}-{logical_id}"[:32]
    # Subnets/SecurityGroups from the template are ignored: the local proxy binds
    # a host port and moto's default VPC supplies subnets.
    lb = elbv2.create_load_balancer(
        Name=name,
        Type=props.get("Type", "application"),
        Scheme=props.get("Scheme", "internet-facing"),
    )
    arn = lb["LoadBalancerArn"]
    full_name = arn.split(":loadbalancer/", 1)[-1]  # e.g. app/<name>/<id>
    return {
        "PhysicalId": arn,
        "Attributes": {
            "DNSName": elbv2.dns_name(arn),
            "LoadBalancerArn": arn,
            "LoadBalancerName": name,
            "LoadBalancerFullName": full_name,
            "CanonicalHostedZoneID": "Z00000000OBLAKO",  # local placeholder
        },
    }


def _elb_lb_delete(physical_id, props):
    _, elbv2 = _ecs_elbv2()
    elbv2.delete_load_balancer(physical_id)


# AWS::ElasticLoadBalancingV2::TargetGroup
def _elb_tg_create(logical_id, props, ctx):
    _, elbv2 = _ecs_elbv2()
    name = props.get("Name") or f"{ctx['stack']}-{logical_id}"[:32]
    kwargs = {
        "Name": name,
        "Port": int(props.get("Port", 80)),
        "Protocol": props.get("Protocol", "HTTP"),
        "TargetType": props.get("TargetType", "ip"),
    }
    if "HealthCheckPath" in props:
        kwargs["HealthCheckPath"] = props["HealthCheckPath"]
    tg = elbv2.create_target_group(**kwargs)
    arn = tg["TargetGroupArn"]
    return {  # Ref -> arn; GetAtt TargetGroupName/TargetGroupFullName
        "PhysicalId": arn,
        "Attributes": {
            "TargetGroupName": name,
            "TargetGroupFullName": arn.split(":", 5)[-1],  # targetgroup/<name>/<id>
        },
    }


def _elb_tg_delete(physical_id, props):
    try:
        _moto_client("elbv2").delete_target_group(TargetGroupArn=physical_id)
    except Exception:  # noqa: BLE001
        pass


# AWS::ElasticLoadBalancingV2::Listener -> wire the LB's proxy to the target group
def _elb_listener_create(logical_id, props, ctx):
    _, elbv2 = _ecs_elbv2()
    listener = elbv2.create_listener(
        LoadBalancerArn=props["LoadBalancerArn"],
        Port=int(props.get("Port", 80)),
        Protocol=props.get("Protocol", "HTTP"),
        DefaultActions=props.get("DefaultActions", []),
    )
    return listener["ListenerArn"]


def _elb_listener_delete(physical_id, props):
    pass  # the proxy is torn down with its load balancer


# AWS::Logs::LogGroup (control plane via moto; local containers log to the backend)
def _logs_create(logical_id, props, ctx):
    name = props.get("LogGroupName") or f"/oblako/{ctx['stack']}/{logical_id}"
    try:
        _moto_client("logs").create_log_group(logGroupName=name)
    except Exception:  # noqa: BLE001 - already exists
        pass
    return name


def _logs_delete(physical_id, props):
    try:
        _moto_client("logs").delete_log_group(logGroupName=physical_id)
    except Exception:  # noqa: BLE001
        pass


# AWS::EC2::SecurityGroup (moto metadata; not enforced for local routing)
def _sg_create(logical_id, props, ctx):
    ec2 = _moto_client("ec2")
    vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    vpc_id = vpc[0]["VpcId"] if vpc else ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
    sg = ec2.create_security_group(
        GroupName=f"{ctx['stack']}-{logical_id}",
        Description=props.get("GroupDescription", "oblako"),
        VpcId=vpc_id,
    )
    return sg["GroupId"]


def _sg_delete(physical_id, props):
    try:
        _moto_client("ec2").delete_security_group(GroupId=physical_id)
    except Exception:  # noqa: BLE001
        pass


# resource_type -> (create, delete)
PROVIDERS = {
    "AWS::S3::Bucket": (_s3_create, _s3_delete),
    "AWS::DynamoDB::Table": (_ddb_create, _ddb_delete),
    "AWS::Redshift::Cluster": (_redshift_create, _redshift_delete),
    "AWS::RDS::DBInstance": (_rds_create, _rds_delete),
    "AWS::IAM::Role": (_iam_create, _iam_delete),
    "AWS::EC2::Instance": (_ec2_create, _ec2_delete),
    "AWS::EC2::SecurityGroup": (_sg_create, _sg_delete),
    "AWS::Lambda::Function": (_lambda_create, _lambda_delete),
    "AWS::ApiGateway::RestApi": (_apigw_create, _apigw_delete),
    "AWS::StepFunctions::StateMachine": (_sfn_create, _sfn_delete),
    "AWS::OpenSearchService::Domain": (_opensearch_create, _opensearch_delete),
    "AWS::Logs::LogGroup": (_logs_create, _logs_delete),
    "AWS::ECS::Cluster": (_ecs_cluster_create, _ecs_cluster_delete),
    "AWS::ECS::TaskDefinition": (_ecs_taskdef_create, _ecs_taskdef_delete),
    "AWS::ECS::Service": (_ecs_service_create, _ecs_service_delete),
    "AWS::ElasticLoadBalancingV2::LoadBalancer": (_elb_lb_create, _elb_lb_delete),
    "AWS::ElasticLoadBalancingV2::TargetGroup": (_elb_tg_create, _elb_tg_delete),
    "AWS::ElasticLoadBalancingV2::Listener": (
        _elb_listener_create,
        _elb_listener_delete,
    ),
}
