"""EC2 service: AWS EC2 control plane (moto) + real container-backed instances.

moto owns the control plane — RunInstances, DescribeInstances, tags, VPCs,
security groups, AMIs — with full describe fidelity. On top of that, oblako backs
each instance with a **real Docker container** and a **Docker named volume as its
EBS root** (``real behavior, simulated topology``: an instance is a container,
EBS is a volume, the VM/Nitro boundary is simulated). This mirrors how Lambda
runs locally (a container per invoke) — EC2 just keeps its container long-lived,
matching a durable instance vs Lambda's ephemeral microVM.

AWS EC2 is full VMs (Nitro); Lambda is Firecracker microVMs. Neither boots on a
Mac, so both map to a container locally — the faithful distinction is lifecycle:
EC2 containers persist until TerminateInstances; Lambda's vanish after the invoke.
A Firecracker backend stays a future bare-metal-Linux fidelity option.
"""

from __future__ import annotations

import os

from .boto import BotoService
from .moto import MotoService

# A generic instance is a long-running container off a base Linux image. AMI ids
# are metadata only (moto), so the image is fixed (override via OBLAKO_EC2_IMAGE,
# e.g. a tiny image for tests).
DEFAULT_INSTANCE_IMAGE = "amazonlinux:2023"
EBS_MOUNT = "/ebs"  # the instance's EBS volume is mounted here
INSTANCE_LABEL = "oblako.ec2.instance-id"


def _docker():
    from .backends import docker_client

    return docker_client()  # honours OBLAKO_CONTAINER_BACKEND (docker/podman/colima)


def _image_for(image_id: str | None) -> str:
    """Resolve the backing container image (AMI id is metadata; env can override)."""
    return os.environ.get("OBLAKO_EC2_IMAGE") or DEFAULT_INSTANCE_IMAGE


def _container_name(instance_id: str) -> str:
    return f"oblako-ec2-{instance_id}"


def _volume_name(instance_id: str) -> str:
    return f"oblako-ec2-{instance_id}"  # the instance's EBS volume


def start_instance_container(
    instance_id: str,
    image_id: str | None = None,
    image: str | None = None,
    command=None,
    published_ports: dict | None = None,
) -> str:
    """Start (idempotently) the backing container + EBS volume for an instance.

    Reusable by both Ec2Service and the CloudFormation AWS::EC2::Instance provider
    so a stack-provisioned instance is just as real as a service-launched one.
    ``image`` forces a specific backing image (e.g. a Jupyter image for a
    SageMaker notebook instance); otherwise the AMI id maps to the default.
    ``published_ports`` maps container ports to host ports (e.g. a notebook
    instance publishing JupyterLab).
    """
    import docker

    client = _docker()
    name = _container_name(instance_id)
    try:  # already backed?
        existing = client.containers.get(name)
        if existing.status != "running":
            existing.start()
        return existing.id
    except docker.errors.NotFound:
        pass
    image = image or _image_for(image_id)
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        client.images.pull(image)
    vol = _volume_name(instance_id)
    try:
        client.volumes.get(vol)
    except docker.errors.NotFound:
        client.volumes.create(vol, labels={INSTANCE_LABEL: instance_id})  # EBS root
    container = client.containers.run(
        image,
        command=command or ["sleep", "infinity"],
        detach=True,
        name=name,
        volumes={vol: {"bind": EBS_MOUNT, "mode": "rw"}},
        ports=published_ports or None,
        extra_hosts={
            "host.docker.internal": "host-gateway"
        },  # reach oblako on the host
        labels={INSTANCE_LABEL: instance_id, "oblako.service": "ec2"},
    )
    return container.id


def terminate_instance_container(instance_id: str) -> None:
    """Remove the backing container and its EBS volume (idempotent)."""
    import docker

    client = _docker()
    try:
        client.containers.get(_container_name(instance_id)).remove(force=True)
    except docker.errors.NotFound:
        pass
    try:
        client.volumes.get(_volume_name(instance_id)).remove(force=True)
    except docker.errors.NotFound:
        pass


@BotoService("ec2")
class Ec2Service:
    """AWS EC2 — moto control plane + container-backed instances."""

    name = "ec2"

    def __init__(self, moto: MotoService):
        """Wire to the shared moto endpoint (moto owns EC2 metadata)."""
        self.moto = moto

    @property
    def endpoint_url(self) -> str:
        """moto serves EC2 at the same endpoint as every other AWS API."""
        return self.moto.endpoint_url

    # Control plane + real compute
    def run_instance(
        self,
        image_id: str = "ami-0abcdef1234567890",
        instance_type: str = "t3.micro",
        *,
        backed: bool = True,
        **kwargs,
    ) -> str:
        """Launch one instance: record it in moto, then back it with a container.

        Returns the InstanceId. ``backed=False`` records control-plane metadata
        only (no container) — useful when Docker isn't available.
        """
        iid = self.get_client().run_instances(
            ImageId=image_id,
            InstanceType=instance_type,
            MinCount=1,
            MaxCount=1,
            **kwargs,
        )["Instances"][0]["InstanceId"]
        if backed:
            start_instance_container(iid, image_id=image_id)
        return iid

    def stop_instance(self, instance_id: str) -> None:
        """Stop the instance (moto -> stopped; container stopped, EBS volume kept)."""
        self.get_client().stop_instances(InstanceIds=[instance_id])
        self._with_container(instance_id, lambda c: c.stop())

    def start_instance(self, instance_id: str) -> None:
        """Start a stopped instance (moto -> running; container started)."""
        self.get_client().start_instances(InstanceIds=[instance_id])
        start_instance_container(instance_id)

    def terminate_instance(self, instance_id: str) -> None:
        """Terminate the instance (moto -> terminated; container + EBS volume removed)."""
        self.get_client().terminate_instances(InstanceIds=[instance_id])
        terminate_instance_container(instance_id)

    def instance_container(self, instance_id: str):
        """Return the backing container (or None if the instance isn't backed)."""
        import docker

        try:
            return _docker().containers.get(_container_name(instance_id))
        except docker.errors.NotFound:
            return None

    def list_instance_containers(self) -> list:
        """All oblako EC2 instance containers (running or stopped)."""
        return _docker().containers.list(
            all=True, filters={"label": "oblako.service=ec2"}
        )

    def _with_container(self, instance_id: str, fn) -> None:
        c = self.instance_container(instance_id)
        if c is not None:
            fn(c)

    # Lifecycle (moto-backed; instance containers are managed per-id, not as one service)
    def start(self) -> None:
        """Bring moto up if it isn't already (no own service container)."""
        self.moto.wait_ready(timeout=2) or self.moto.start()

    def stop(self) -> None:
        """No-op — moto owns the control plane; instances are managed per-id."""

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Defer readiness to moto."""
        return self.moto.wait_ready(timeout=timeout)

    def status(self):
        """Defer status to moto."""
        return self.moto.status()
