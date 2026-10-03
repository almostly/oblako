"""Unit tests for the MWAA API's validation, errors and tags (no Docker)."""

import pytest
from starlette.testclient import TestClient

from oblako.engines.mwaa import create_app
from oblako.engines.mwaa import environments as envs


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(envs, "STATE", tmp_path / "environments.json")
    monkeypatch.setattr(envs, "HOME", tmp_path)
    return TestClient(create_app())


def _put(client, name, **body):
    return client.put(f"/environments/{name}", json=body)


def test_create_requires_source_and_role(client):
    resp = _put(client, "etl", AirflowVersion="3.3.1")
    assert resp.status_code == 400
    assert resp.headers["x-amzn-errortype"] == "ValidationException"


def test_create_refuses_an_unknown_airflow_version(client):
    resp = _put(
        client,
        "etl",
        AirflowVersion="1.10.12",
        SourceBucketArn="arn:aws:s3:::dags",
        DagS3Path="dags",
        ExecutionRoleArn="arn:aws:iam::123456789012:role/mwaa",
    )
    assert resp.status_code == 400
    assert "1.10.12" in resp.json()["message"]


def test_unknown_environment_is_not_found(client):
    for resp in (
        client.get("/environments/nope"),
        client.delete("/environments/nope"),
        client.patch("/environments/nope", json={}),
        client.post("/restapi/nope", json={"Path": "/dags", "Method": "GET"}),
    ):
        assert resp.status_code == 404
        assert resp.headers["x-amzn-errortype"] == "ResourceNotFoundException"


def test_tags_and_rest_api_on_a_recorded_environment(client):
    envs._update(
        "etl",
        {
            "Name": "etl",
            "Arn": envs.arn("etl"),
            "Status": "CREATING",
            "AirflowVersion": "3.3.1",
            "port": 1,
        },
        create=True,
    )
    arn = envs.arn("etl")
    assert client.post(f"/tags/{arn}", json={"Tags": {"team": "data"}}).json() == {}
    assert client.get(f"/tags/{arn}").json() == {"Tags": {"team": "data"}}
    client.delete(f"/tags/{arn}", params={"tagKeys": "team"})
    assert client.get(f"/tags/{arn}").json() == {"Tags": {}}
    # an environment that is still creating refuses InvokeRestApi
    resp = client.post("/restapi/etl", json={"Path": "/dags", "Method": "GET"})
    assert resp.status_code == 400
    assert client.get("/environments").json() == {"Environments": ["etl"]}
    env = client.get("/environments/etl").json()["Environment"]
    assert env["Status"] == "CREATING" and "port" not in env


def test_cli_token_is_not_simulated(client):
    resp = client.post("/clitoken/etl")
    assert resp.status_code == 400
    assert "InvokeRestApi" in resp.json()["message"]


def test_defaults_match_what_mwaa_reports_for_micro():
    # observed on AWS MWAA: mw1.micro, Airflow 3.3.1, no settings given
    got = envs.defaults("etl", {"EnvironmentClass": "mw1.micro"})
    assert (got["MinWorkers"], got["MaxWorkers"], got["Schedulers"]) == (1, 1, 1)
    assert (got["MinWebservers"], got["MaxWebservers"]) == (1, 1)
    assert got["EndpointManagement"] == "SERVICE"
    logs = got["LoggingConfiguration"]
    assert logs["TaskLogs"]["Enabled"] and not logs["SchedulerLogs"]["Enabled"]
    assert logs["TaskLogs"]["CloudWatchLogGroupArn"].endswith(":airflow-etl-Task")


def test_rest_api_responses_drop_null_fields():
    body = {"dag_id": "d", "note": None, "conf": {"a": 1, "b": None}, "xs": [None]}
    assert envs._without_nulls(body) == {"dag_id": "d", "conf": {"a": 1}, "xs": [None]}


def test_not_found_message_matches_aws(client):
    resp = client.get("/environments/nope")
    assert resp.json()["message"] == "Environment nope not found."
