"""Integration test: an MWAA environment runs a DAG that writes to oblako's S3.

Requires Docker and S3Proxy, and builds AWS's Airflow image on first use (it
takes a while), so it carries the ``mwaa`` marker and runs on request only:
    pytest -m mwaa
The engine starts in-process on a free port. The DAG's task uses boto3 with no
endpoint in code, so it also checks that tasks reach oblako's services.
"""

import time

import boto3
import pytest

from oblako.engines import mwaa
from oblako.services import S3ProxyService
from tests.ports import free_port

pytestmark = pytest.mark.mwaa

NAME = "pytest-mwaa"
BUCKET = "pytest-mwaa-source"
DAG = """
import datetime

import boto3
from airflow.sdk import dag, task


@dag(schedule=None, start_date=datetime.datetime(2026, 1, 1), catchup=False)
def write_marker():
    @task
    def write():
        boto3.client("s3").put_object(
            Bucket="pytest-mwaa-source", Key="out/marker.txt", Body=b"ran"
        )

    write()


write_marker()
"""


@pytest.fixture(scope="module")
def environment():
    s3 = S3ProxyService().get_client()
    if BUCKET not in [b["Name"] for b in s3.list_buckets()["Buckets"]]:
        s3.create_bucket(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key="dags/write_marker.py", Body=DAG.encode())
    url = mwaa.start_in_thread(free_port())
    client = boto3.client(
        "mwaa",
        endpoint_url=url,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    try:
        client.delete_environment(Name=NAME)
    except client.exceptions.ResourceNotFoundException:
        pass
    client.create_environment(
        Name=NAME,
        AirflowVersion="3.3.1",
        SourceBucketArn=f"arn:aws:s3:::{BUCKET}",
        DagS3Path="dags",
        ExecutionRoleArn="arn:aws:iam::123456789012:role/mwaa",
        NetworkConfiguration={"SubnetIds": ["subnet-1", "subnet-2"]},
    )
    deadline = time.time() + 1800
    status = "CREATING"
    while time.time() < deadline:
        env = client.get_environment(Name=NAME)["Environment"]
        status = env["Status"]
        if status != "CREATING":
            break
        time.sleep(10)
    assert status == "AVAILABLE", env.get("LastUpdate")
    yield client, s3
    client.delete_environment(Name=NAME)
    for obj in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []):
        s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
    s3.delete_bucket(Bucket=BUCKET)


def _rest(client, path, method="GET", body=None):
    kwargs = {"Body": body} if body is not None else {}
    return client.invoke_rest_api(Name=NAME, Path=path, Method=method, **kwargs)


def test_dag_from_s3_runs_and_writes_to_s3(environment):
    client, s3 = environment
    deadline = time.time() + 300
    while time.time() < deadline:  # the DAG processor parses the synced file
        dags = _rest(client, "/dags")["RestApiResponse"]["dags"]
        if any(d["dag_id"] == "write_marker" for d in dags):
            break
        time.sleep(5)
    _rest(client, "/dags/write_marker", "PATCH", {"is_paused": False})
    run = _rest(
        client,
        "/dags/write_marker/dagRuns",
        "POST",
        {"logical_date": None},
    )["RestApiResponse"]
    state = run["state"]
    deadline = time.time() + 300
    while state not in ("success", "failed") and time.time() < deadline:
        time.sleep(5)
        state = _rest(client, f"/dags/write_marker/dagRuns/{run['dag_run_id']}")[
            "RestApiResponse"
        ]["state"]
    assert state == "success"
    marker = s3.get_object(Bucket=BUCKET, Key="out/marker.txt")["Body"].read()
    assert marker == b"ran"


def test_unknown_rest_path_is_a_client_exception(environment):
    client, _ = environment
    with pytest.raises(client.exceptions.RestApiClientException) as err:
        _rest(client, "/dags/no_such_dag")
    assert err.value.response["RestApiStatusCode"] == 404
