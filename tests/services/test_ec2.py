"""Integration tests for EC2 — moto control plane + container-backed instances.

Requires moto + Docker (oblako up). An instance is backed by a real container and
a Docker named volume (EBS); we use a tiny image (alpine) via OBLAKO_EC2_IMAGE so
the test doesn't pull amazonlinux:2023.
"""

from __future__ import annotations

import os

import pytest

from oblako.services import ec2 as ec2mod
from oblako.services.ec2 import Ec2Service
from oblako.services.moto import MotoService


@pytest.fixture(scope="module")
def ec2():
    moto = MotoService()
    if not moto.wait_ready(timeout=5):
        pytest.skip("moto is not running on :5500")
    try:
        ec2mod._docker().ping()
    except Exception:  # noqa: BLE001
        pytest.skip("Docker is not available")
    os.environ["OBLAKO_EC2_IMAGE"] = "alpine:3"  # tiny backing image for the test
    yield Ec2Service(moto=moto)
    os.environ.pop("OBLAKO_EC2_IMAGE", None)


def test_control_plane_run_and_describe(ec2):
    iid = ec2.run_instance(instance_type="t3.medium", backed=False)  # metadata only
    inst = ec2.get_client().describe_instances(InstanceIds=[iid])[
        "Reservations"][0]["Instances"][0]
    assert iid.startswith("i-") and inst["InstanceType"] == "t3.medium"
    ec2.get_client().terminate_instances(InstanceIds=[iid])


def test_instance_is_backed_by_real_container_and_volume(ec2):
    iid = ec2.run_instance(instance_type="t3.micro")  # backed=True (default)
    try:
        c = ec2.instance_container(iid)
        assert c is not None and c.status == "running"
        # EBS volume exists and is mounted at /ebs
        mounts = {m["Destination"] for m in c.attrs["Mounts"]}
        assert ec2mod.EBS_MOUNT in mounts
        # stop -> container stops, volume survives; start -> back to running
        ec2.stop_instance(iid)
        ec2.instance_container(iid).reload()
        assert ec2.instance_container(iid).status in ("exited", "created")
        ec2.start_instance(iid)
        ec2.instance_container(iid).reload()
        assert ec2.instance_container(iid).status == "running"
    finally:
        ec2.terminate_instance(iid)
    # terminate removes both container and EBS volume
    assert ec2.instance_container(iid) is None
    import docker
    with pytest.raises(docker.errors.NotFound):
        ec2mod._docker().volumes.get(ec2mod._volume_name(iid))
