"""Integration tests for the EC2 control plane (moto-backed).

Requires moto running (oblako up, or docker compose up -d moto). Like IAM/Lambda,
EC2 is a thin boto3 entry point over moto — instances are metadata (describe
fidelity), no compute runs.
"""

from __future__ import annotations

import pytest

from oblako.services.ec2 import Ec2Service
from oblako.services.moto import MotoService


@pytest.fixture(scope="module")
def ec2():
    moto = MotoService()
    if not moto.wait_ready(timeout=5):
        pytest.skip("moto is not running on :5500")
    return Ec2Service(moto=moto)


def test_run_and_describe_instance(ec2):
    iid = ec2.run_instance(instance_type="t3.medium")
    assert iid.startswith("i-")
    client = ec2.get_client()
    inst = client.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    assert inst["InstanceType"] == "t3.medium"
    assert inst["State"]["Name"] in ("running", "pending")
    # tags round-trip
    client.create_tags(Resources=[iid], Tags=[{"Key": "Name", "Value": "oblako-ec2-test"}])
    tags = {t["Key"]: t["Value"]
            for t in client.describe_instances(InstanceIds=[iid])
            ["Reservations"][0]["Instances"][0].get("Tags", [])}
    assert tags.get("Name") == "oblako-ec2-test"
    client.terminate_instances(InstanceIds=[iid])
