"""Deploy an ECS Fargate service behind an ALB onto oblako, with CloudFormation.

The stock `infra/ecs-fargate-alb.yaml` (an ECS Cluster + Fargate Service + an
Application Load Balancer) deploys to oblako's local CloudFormation. Each task
becomes a **real container** and the ALB becomes a **real Caddy reverse proxy**,
so the stack's `ServiceURL` output is a curlable local endpoint backed by your
actual FastAPI image, load-balanced across `DesiredCount` containers.

Prerequisites:
    make up                                  # moto control plane (:5500) + Docker
    docker build -t decision-service:latest .   # build the local image (run in this dir)
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from oblako.services import Oblako

TEMPLATE = (Path(__file__).parent / "infra" / "ecs-fargate-alb.yaml").read_text()
STACK = "decision-service"


def _wait_url(url: str, attempts: int = 45) -> None:
    for _ in range(attempts):
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except Exception:  # service still coming up
            time.sleep(1)
    raise TimeoutError(f"{url} never served")


def main() -> None:
    o = Oblako()
    o.moto.wait_ready(timeout=20)
    cfn = o.cloudformation.get_client()  # auto-starts the in-process CFN server

    # Vpc/Subnets are required by the template's types but ignored locally
    # (oblako uses moto's default VPC), so any placeholder value works.
    cfn.create_change_set(
        StackName=STACK,
        TemplateBody=TEMPLATE,
        ChangeSetName="cs1",
        ChangeSetType="CREATE",
        Capabilities=["CAPABILITY_NAMED_IAM"],
        Parameters=[
            {"ParameterKey": "VpcId", "ParameterValue": "vpc-local"},
            {"ParameterKey": "SubnetIds", "ParameterValue": "subnet-a,subnet-b"},
            {
                "ParameterKey": "ContainerImage",
                "ParameterValue": "decision-service:latest",
            },
        ],
    )
    cfn.get_waiter("change_set_create_complete").wait(
        StackName=STACK, ChangeSetName="cs1"
    )
    cfn.execute_change_set(StackName=STACK, ChangeSetName="cs1")
    cfn.get_waiter("stack_create_complete").wait(StackName=STACK)
    print("stack CREATE_COMPLETE")

    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=STACK)["Stacks"][0].get("Outputs", [])
    }
    url = outputs["ServiceURL"]
    print("ServiceURL:", url)

    _wait_url(url + "/health")
    print("health:", urllib.request.urlopen(url + "/health").read().decode())

    body = json.dumps(
        {
            "application_id": "app-9",
            "age": 40,
            "monthly_income": 3000,
            "requested_amount": 40000,
            "dti": 0.55,
            "utilization": 0.4,
            "days_past_due": 30,
            "kyc_passed": False,
        }
    ).encode()
    req = urllib.request.Request(
        url + "/decision", data=body, headers={"content-type": "application/json"}
    )
    print("decision:", urllib.request.urlopen(req).read().decode())

    print("\nTear down with: cfn.delete_stack(StackName='decision-service')")


if __name__ == "__main__":
    main()
