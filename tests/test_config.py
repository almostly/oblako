"""Unit tests for central region/account config and client threading."""

from oblako import config


def test_defaults():
    assert config.region() == "us-east-1"
    assert config.account_id() == "123456789012"  # moto's default account


def test_env_override(monkeypatch):
    monkeypatch.setenv("OBLAKO_REGION", "eu-west-1")
    monkeypatch.setenv("OBLAKO_ACCOUNT_ID", "222222222222")
    assert config.region() == "eu-west-1"
    assert config.account_id() == "222222222222"


def test_arn():
    assert config.arn("s3", "my-bucket") == "arn:aws:s3:us-east-1:123456789012:my-bucket"
    assert config.arn("iam", "role/r", region_scoped=False) == "arn:aws:iam::123456789012:role/r"


def test_clients_use_configured_region(monkeypatch):
    monkeypatch.setenv("OBLAKO_REGION", "ap-south-1")
    from oblako.services.dynamodb import DynamoDBService
    from oblako.services.s3proxy import S3ProxyService
    from oblako.services.stepfunctions import StepFunctionsService

    assert DynamoDBService().get_client().meta.region_name == "ap-south-1"
    assert S3ProxyService().get_client().meta.region_name == "ap-south-1"
    assert StepFunctionsService().get_client().meta.region_name == "ap-south-1"
