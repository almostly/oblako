"""Unit tests for the `oblako notebook` wiring (no JupyterLab / Docker / services)."""

import json
import pathlib

import boto3

from oblako.notebook import (
    ENDPOINTS,
    is_running,
    make_env,
    starter_notebook,
    write_starter,
)


def test_make_env_wires_endpoints_creds_and_config(tmp_path):
    env = make_env(tmp_path)
    assert env["AWS_ENDPOINT_URL_S3"] == "http://localhost:9000"
    assert env["AWS_ENDPOINT_URL_DYNAMODB"] == "http://localhost:8007"
    assert env["AWS_ENDPOINT_URL_DYNAMODB_STREAMS"] == "http://localhost:8001"
    assert env["AWS_ACCESS_KEY_ID"] == "test"
    assert env["AWS_REQUEST_CHECKSUM_CALCULATION"] == "when_required"
    cfg = pathlib.Path(env["AWS_CONFIG_FILE"])
    assert "addressing_style = path" in cfg.read_text()


def test_endpoint_env_vars_redirect_unmodified_boto3(monkeypatch):
    # the whole point: plain boto3 (no endpoint_url) resolves to the local services
    for env, url in ENDPOINTS.items():
        monkeypatch.setenv(env, url)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "t")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "t")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    expected = {
        "s3": "http://localhost:9000",
        "dynamodb": "http://localhost:8007",  # the proxy: vector search, tags
        "dynamodbstreams": "http://localhost:8001",  # DynamoDB Local itself
        "stepfunctions": "http://localhost:8083",  # serviceId "SFN"
        "cloudformation": "http://localhost:8017",
        "lambda": "http://localhost:5500",
        "apigateway": "http://localhost:5500",  # serviceId "API Gateway"
    }
    for svc, url in expected.items():
        assert boto3.client(svc).meta.endpoint_url == url, svc


def test_starter_notebook_is_valid_nbformat():
    nb = starter_notebook()
    assert nb["nbformat"] == 4
    assert nb["cells"]
    assert any(c["cell_type"] == "code" for c in nb["cells"])
    json.dumps(nb)  # must be JSON-serializable


def test_write_starter_creates_valid_notebook(tmp_path):
    path = write_starter(tmp_path)
    assert path.exists() and path.name == "oblako-welcome.ipynb"
    json.loads(path.read_text())  # valid JSON
    # idempotent: a second call doesn't clobber
    path.write_text('{"edited": true}')
    write_starter(tmp_path)
    assert json.loads(path.read_text()) == {"edited": True}


def test_is_running_false_when_nothing_listening():
    assert is_running(port=59999, timeout=0.2) is False
