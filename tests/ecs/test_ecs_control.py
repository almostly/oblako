"""Integration test: plain boto3 RunTask runs a real container through ecs-control.

Requires moto and Docker. The proxy starts in-process on a free port; task
operations run containers, every other call goes to moto.
"""

from __future__ import annotations

import uuid

import boto3
import pytest

from oblako.engines import ecs_control
from tests.ecs.test_ecs import _ecs_up
from tests.ports import free_port

IMAGE = "public.ecr.aws/docker/library/busybox:1.36"


@pytest.fixture(scope="module")
def ecs():
    url = ecs_control.start_in_thread(free_port())
    client = boto3.client(
        "ecs",
        endpoint_url=url,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    cluster = f"ctl-{uuid.uuid4().hex[:6]}"
    client.create_cluster(clusterName=cluster)
    yield client, cluster
    for arn in client.list_tasks(cluster=cluster)["taskArns"]:
        client.stop_task(cluster=cluster, task=arn)
    client.delete_cluster(cluster=cluster)


def _task_definition(client, family, command):
    client.register_task_definition(
        family=family,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu="256",
        memory="512",
        containerDefinitions=[
            {"name": "job", "image": IMAGE, "command": command, "essential": True}
        ],
    )


def _run(client, cluster, family):
    task = client.run_task(
        cluster=cluster,
        taskDefinition=family,
        launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {"subnets": ["subnet-a"]}},
    )["tasks"][0]
    client.get_waiter("tasks_stopped").wait(
        cluster=cluster,
        tasks=[task["taskArn"]],
        WaiterConfig={"Delay": 1, "MaxAttempts": 120},
    )
    return client.describe_tasks(cluster=cluster, tasks=[task["taskArn"]])["tasks"][0]


@pytest.mark.integration
@pytest.mark.skipif(not _ecs_up(), reason="moto + Docker not running")
def test_run_task_runs_the_container_and_reports_its_exit_code(ecs):
    client, cluster = ecs
    _task_definition(client, "ok-job", ["sh", "-c", "exit 0"])
    _task_definition(client, "failing-job", ["sh", "-c", "exit 3"])
    ok = _run(client, cluster, "ok-job")
    assert ok["lastStatus"] == "STOPPED"
    assert ok["containers"] == [{"name": "job", "lastStatus": "STOPPED", "exitCode": 0}]
    assert ":task-definition/ok-job:" in ok["taskDefinitionArn"]
    assert ok["taskArn"].startswith("arn:aws:ecs:us-east-1:123456789012:task/")
    failed = _run(client, cluster, "failing-job")
    assert failed["containers"][0]["exitCode"] == 3
    assert failed["stoppedReason"] == "Essential container in task exited"


@pytest.mark.integration
@pytest.mark.skipif(not _ecs_up(), reason="moto + Docker not running")
def test_other_calls_go_to_moto_and_unknown_tasks_are_missing(ecs):
    client, cluster = ecs
    clusters = client.describe_clusters(clusters=[cluster])["clusters"]
    assert clusters[0]["clusterName"] == cluster
    missing = "arn:aws:ecs:us-east-1:123456789012:task/x/none"
    resp = client.describe_tasks(cluster=cluster, tasks=[missing])
    assert resp["tasks"] == [] and resp["failures"][0]["reason"] == "MISSING"
