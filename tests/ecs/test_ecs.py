"""Tests for ECS (Fargate) + ELBv2.

The unit test covers the CloudFormation task-definition translation (no services
needed). The integration test runs real task containers behind a real ALB and is
skipped unless moto + Docker are available:
    make up
"""

from __future__ import annotations

import time
import urllib.request
import uuid

import pytest

from oblako.engines.cloudformation.providers import _cfn_taskdef_to_boto

WHOAMI = "traefik/whoami"  # tiny image, serves HTTP 200 on any path


def test_cfn_taskdef_pascalcase_to_boto_camelcase():
    boto = _cfn_taskdef_to_boto(
        {
            "Family": "svc",
            "RequiresCompatibilities": ["FARGATE"],
            "NetworkMode": "awsvpc",
            "Cpu": 256,
            "Memory": 512,
            "ExecutionRoleArn": "arn:aws:iam::0:role/x",
            "RuntimePlatform": {
                "CpuArchitecture": "ARM64",
                "OperatingSystemFamily": "LINUX",
            },
            "ContainerDefinitions": [
                {
                    "Name": "web",
                    "Image": "img:latest",
                    "PortMappings": [{"ContainerPort": 8000, "Protocol": "tcp"}],
                    "Environment": [{"Name": "K", "Value": "V"}],
                    "Command": ["run"],
                    "LogConfiguration": {"LogDriver": "awslogs"},
                }
            ],
        }
    )
    assert boto["family"] == "svc"
    assert boto["requiresCompatibilities"] == ["FARGATE"]
    assert boto["networkMode"] == "awsvpc"
    assert boto["cpu"] == "256" and boto["memory"] == "512"
    assert boto["executionRoleArn"] == "arn:aws:iam::0:role/x"
    assert boto["runtimePlatform"] == {
        "cpuArchitecture": "ARM64",
        "operatingSystemFamily": "LINUX",
    }
    c = boto["containerDefinitions"][0]
    assert c["name"] == "web" and c["image"] == "img:latest"
    assert c["portMappings"] == [{"containerPort": 8000, "protocol": "tcp"}]
    assert c["environment"] == [{"name": "K", "value": "V"}]
    assert c["command"] == ["run"]
    assert "logConfiguration" not in c  # dropped: local containers log to the backend


def _ecs_up() -> bool:
    try:
        from oblako.services import Oblako
        from oblako.services.backends import docker_client

        if not Oblako().moto.wait_ready(timeout=3):
            return False
        docker_client().ping()
        return True
    except Exception:
        return False


def _get_status(url: str, attempts: int = 45) -> int:
    last = None
    for _ in range(attempts):
        try:
            return urllib.request.urlopen(url, timeout=2).status
        except Exception as e:  # noqa: BLE001 - container still coming up
            last = e
            time.sleep(1)
    raise AssertionError(f"{url} never served ({last})")


@pytest.mark.integration
@pytest.mark.skipif(not _ecs_up(), reason="moto + Docker not running")
def test_run_task_and_service_behind_alb():
    from oblako.services import Oblako

    o = Oblako()
    ecs, elb = o.ecs, o.elbv2
    sfx = uuid.uuid4().hex[:6]
    family = f"itest-{sfx}"
    ecs.register_task_definition(
        family=family,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu="256",
        memory="512",
        containerDefinitions=[
            {
                "name": "web",
                "image": WHOAMI,
                "portMappings": [{"containerPort": 80, "protocol": "tcp"}],
            }
        ],
    )

    # 1. run_task launches a real container, reachable on its host port,
    #    and (awsvpc) carries an ENI attachment.
    run = ecs.run_task(family, count=1)
    task = run["tasks"][0]["taskArn"]
    try:
        assert _get_status(ecs.task_url(task)) == 200
        assert run["tasks"][0]["attachments"]  # awsvpc ENI metadata
        assert ecs.describe_tasks(tasks=[task])["tasks"][0]["lastStatus"] == "RUNNING"
    finally:
        ecs.stop_task(task)
    assert task not in ecs.list_tasks()

    # 2. a service of 2 tasks behind a real ALB; the LB URL routes to the tasks.
    lb_arn = elb.create_load_balancer(Name=f"alb-{sfx}", Type="application")[
        "LoadBalancerArn"
    ]
    tg_arn = elb.create_target_group(
        Name=f"tg-{sfx}", Port=80, Protocol="HTTP", TargetType="ip"
    )["TargetGroupArn"]
    elb.create_listener(
        LoadBalancerArn=lb_arn,
        Port=80,
        Protocol="HTTP",
        DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
    )
    svc = f"svc-{sfx}"
    try:
        ecs.create_service(
            service_name=svc,
            task_definition=family,
            desired_count=2,
            load_balancers=[
                {"targetGroupArn": tg_arn, "containerName": "web", "containerPort": 80}
            ],
        )
        assert _get_status(elb.lb_url(lb_arn)) == 200
        assert len(ecs.list_tasks()) >= 2
    finally:
        ecs.delete_service(svc)
        elb.delete_load_balancer(lb_arn)
