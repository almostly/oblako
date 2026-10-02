"""The common moto-backed services are surfaced and work via the moto endpoint.

oblako runs the full moto image, which serves Secrets Manager, SSM, STS, KMS,
CloudWatch Logs/metrics, EventBridge, and ECR. These are exposed in the notebook
env (AWS_ENDPOINT_URL_*) so unmodified boto3 code that uses them works. The
env-map assertions need nothing; the round-trips need Docker (moto).
"""

import boto3
import pytest
import uuid


def test_notebook_env_exposes_moto_services():
    from oblako.notebook import ENDPOINTS as env

    for name in (
        "AWS_ENDPOINT_URL_SECRETS_MANAGER",
        "AWS_ENDPOINT_URL_SSM",
        "AWS_ENDPOINT_URL_STS",
        "AWS_ENDPOINT_URL_KMS",
        "AWS_ENDPOINT_URL_CLOUDWATCH_LOGS",
        "AWS_ENDPOINT_URL_CLOUDWATCH",
        "AWS_ENDPOINT_URL_EVENTBRIDGE",
        "AWS_ENDPOINT_URL_ECR",
        "AWS_ENDPOINT_URL_ECS",
        "AWS_ENDPOINT_URL_EKS",
    ):
        assert name in env, name


@pytest.fixture(scope="module")
def moto_endpoint():
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker not available")
    from oblako.services import MotoService

    svc = MotoService()
    try:
        svc.start()
    except Exception as err:
        pytest.skip(f"moto unavailable: {err}")
    return svc.endpoint_url


def _client(service, endpoint):
    return boto3.client(
        service,
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_secrets_manager(moto_endpoint):
    sm = _client("secretsmanager", moto_endpoint)
    name = f"oblako/db-{uuid.uuid4().hex[:8]}"
    sm.create_secret(Name=name, SecretString="s3cr3t")
    assert sm.get_secret_value(SecretId=name)["SecretString"] == "s3cr3t"


def test_ssm_parameter_store(moto_endpoint):
    ssm = _client("ssm", moto_endpoint)
    pname = f"/oblako/mode-{uuid.uuid4().hex[:8]}"
    ssm.put_parameter(Name=pname, Value="local", Type="String")
    assert ssm.get_parameter(Name=pname)["Parameter"]["Value"] == "local"


def test_sts_identity(moto_endpoint):
    assert _client("sts", moto_endpoint).get_caller_identity()["Account"]


def test_kms_encrypt_decrypt_roundtrip(moto_endpoint):
    kms = _client("kms", moto_endpoint)
    key_id = kms.create_key()["KeyMetadata"]["KeyId"]
    blob = kms.encrypt(KeyId=key_id, Plaintext=b"hello")["CiphertextBlob"]
    assert kms.decrypt(CiphertextBlob=blob)["Plaintext"] == b"hello"


def test_cloudwatch_logs(moto_endpoint):
    logs = _client("logs", moto_endpoint)
    group = f"/oblako/svc-{uuid.uuid4().hex[:8]}"
    logs.create_log_group(logGroupName=group)
    # filter by prefix: describe_log_groups pages, and a long-lived moto holds many groups
    found = logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]
    assert [g["logGroupName"] for g in found] == [group]


def test_ecr_repository(moto_endpoint):
    ecr = _client("ecr", moto_endpoint)
    repo = ecr.create_repository(repositoryName=f"oblako/img-{uuid.uuid4().hex[:8]}")[
        "repository"
    ]
    assert repo["repositoryUri"]


def test_eventbridge_rule(moto_endpoint):
    events = _client("events", moto_endpoint)
    events.put_rule(Name="oblako-rule", EventPattern='{"source":["oblako"]}')
    assert any(r["Name"] == "oblako-rule" for r in events.list_rules()["Rules"])


def test_ecs_control_plane(moto_endpoint):
    ecs = _client("ecs", moto_endpoint)
    cluster = f"oblako-ecs-{uuid.uuid4().hex[:8]}"
    ecs.create_cluster(clusterName=cluster)
    ecs.register_task_definition(
        family="scorer",
        containerDefinitions=[
            {"name": "app", "image": "oblako-app:latest", "memory": 256}
        ],
    )
    assert "scorer" in [
        arn.split("/")[-1].split(":")[0]
        for arn in ecs.list_task_definitions()["taskDefinitionArns"]
    ]
    clusters = ecs.describe_clusters(clusters=[cluster])["clusters"]
    assert clusters[0]["clusterName"] == cluster


def test_eks_control_plane(moto_endpoint):
    eks = _client("eks", moto_endpoint)
    name = f"oblako-eks-{uuid.uuid4().hex[:8]}"
    eks.create_cluster(
        name=name,
        roleArn="arn:aws:iam::123456789012:role/eks",
        resourcesVpcConfig={},
    )
    cluster = eks.describe_cluster(name=name)["cluster"]
    assert cluster["name"] == name
    assert cluster["status"] == "ACTIVE"
    assert name in eks.list_clusters()["clusters"]
