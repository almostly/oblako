"""EC2 service: AWS EC2 control plane via moto.

moto serves a comprehensive EC2 control plane — RunInstances, DescribeInstances,
Start/StopInstances, tags, VPCs, subnets, security groups, key pairs, AMIs. Like
``IamService`` and ``LambdaService``, this is a thin boto3 entry point over the
shared moto endpoint; there's no separate container (moto owns the state).

Scope: instances are moto metadata — full describe-fidelity, real boto3 behavior,
but no compute runs (an instance is a record, not a VM). Container-backed
"real compute" instances (an instance == a Docker container, EBS == a Docker
volume) are a later stage; this control plane is what stacks and the dashboard
need first, and it composes into CloudFormation via AWS::EC2::Instance.
"""

from __future__ import annotations

from .boto import BotoService
from .moto import MotoService


@BotoService("ec2")
class Ec2Service:
    """AWS EC2 — control plane via moto (no own container)."""

    name = "ec2"

    def __init__(self, moto: MotoService):
        """Wire to the shared moto endpoint (moto owns EC2 state)."""
        self.moto = moto

    @property
    def endpoint_url(self) -> str:
        """moto serves EC2 at the same endpoint as every other AWS API."""
        return self.moto.endpoint_url

    def run_instance(self, image_id: str = "ami-0abcdef1234567890",
                     instance_type: str = "t3.micro", **kwargs) -> str:
        """Launch one instance and return its InstanceId (convenience wrapper)."""
        resp = self.get_client().run_instances(
            ImageId=image_id, InstanceType=instance_type,
            MinCount=1, MaxCount=1, **kwargs,
        )
        return resp["Instances"][0]["InstanceId"]

    def start(self) -> None:
        """Bring moto up if it isn't already (no own container)."""
        self.moto.wait_ready(timeout=2) or self.moto.start()

    def stop(self) -> None:
        """No-op — moto owns the lifecycle."""

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Defer readiness to moto."""
        return self.moto.wait_ready(timeout=timeout)

    def status(self):
        """Defer status to moto."""
        return self.moto.status()
