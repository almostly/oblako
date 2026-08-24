"""The common moto-backed services are surfaced and work via the moto endpoint.

oblako runs the full moto image, which serves Secrets Manager, SSM, STS, KMS,
CloudWatch Logs/metrics, EventBridge, and ECR. These are exposed in the notebook
env (AWS_ENDPOINT_URL_*) so unmodified boto3 code that uses them works. The
env-map assertions need nothing; the round-trips need Docker (moto).
"""

import boto3
import pytest


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
    except Exception as err:  # noqa: BLE001
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
    sm.create_secret(Name="oblako/db", SecretString="s3cr3t")
    assert sm.get_secret_value(SecretId="oblako/db")["SecretString"] == "s3cr3t"


def test_ssm_parameter_store(moto_endpoint):
    ssm = _client("ssm", moto_endpoint)
    ssm.put_parameter(Name="/oblako/mode", Value="local", Type="String")
    assert ssm.get_parameter(Name="/oblako/mode")["Parameter"]["Value"] == "local"


def test_sts_identity(moto_endpoint):
    assert _client("sts", moto_endpoint).get_caller_identity()["Account"]


def test_kms_encrypt_decrypt_roundtrip(moto_endpoint):
    kms = _client("kms", moto_endpoint)
    key_id = kms.create_key()["KeyMetadata"]["KeyId"]
    blob = kms.encrypt(KeyId=key_id, Plaintext=b"hello")["CiphertextBlob"]
    assert kms.decrypt(CiphertextBlob=blob)["Plaintext"] == b"hello"


def test_cloudwatch_logs(moto_endpoint):
    logs = _client("logs", moto_endpoint)
    logs.create_log_group(logGroupName="/oblako/svc")
    names = [g["logGroupName"] for g in logs.describe_log_groups()["logGroups"]]
    assert "/oblako/svc" in names


def test_ecr_repository(moto_endpoint):
    ecr = _client("ecr", moto_endpoint)
    repo = ecr.create_repository(repositoryName="oblako/img")["repository"]
    assert repo["repositoryUri"]


def test_eventbridge_rule(moto_endpoint):
    events = _client("events", moto_endpoint)
    events.put_rule(Name="oblako-rule", EventPattern='{"source":["oblako"]}')
    assert any(r["Name"] == "oblako-rule" for r in events.list_rules()["Rules"])
