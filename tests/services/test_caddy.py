"""Unit tests for CaddyService vanity hostnames + Caddyfile generation."""

from oblako.services.caddy import _caddyfile, vanity_host, vanity_routes


def test_vanity_host_aws_shape():
    # AWS-shaped: <service>-oblako.<account>.<region>.experiments.sagemaker.aws
    host = vanity_host("mlflow")
    assert host.startswith("mlflow-oblako.")
    assert host.endswith(".experiments.sagemaker.aws")


def test_vanity_routes_follow_account_and_region(monkeypatch):
    monkeypatch.setenv("OBLAKO_ACCOUNT_ID", "222222222222")
    monkeypatch.setenv("OBLAKO_REGION", "eu-west-1")
    routes = vanity_routes()
    assert any("222222222222.eu-west-1" in h for h in routes)


def test_caddyfile_rewrites_host_for_mlflow():
    routes = {"mlflow.test.aws": "host.docker.internal:5050"}
    out = _caddyfile(routes)
    assert "http://mlflow.test.aws" in out
    assert "reverse_proxy host.docker.internal:5050" in out
    # MLflow 3's DNS-rebinding protection rejects the AWS hostname; the proxy
    # rewrites the upstream Host header so the request passes.
    assert "header_up Host localhost" in out
