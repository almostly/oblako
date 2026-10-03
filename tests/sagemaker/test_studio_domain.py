"""Integration test for the SageMaker Studio domain (CloudFormation S+EC2+EBS).

Verifies the domain orchestration — create_domain deploys a CFN stack that
provisions a real S3 artifacts bucket + an EC2 notebook instance (container) with
an EBS volume, and delete_domain tears it down. Uses a tiny backing image
(alpine) so it doesn't build/pull the real JupyterLab image — the in-instance
JupyterLab itself is exercised separately (needs the slim notebook image).
"""

from __future__ import annotations

import pytest

from oblako.services import ec2 as ec2mod
from oblako.services import sagemaker as smmod
from oblako.services.moto import MotoService
from oblako.services.s3proxy import S3ProxyService
from oblako.services.sagemaker import SageMakerService


@pytest.fixture
def sm(monkeypatch):
    if not MotoService().wait_ready(timeout=5):
        pytest.skip("moto is not running on :5500")
    try:
        ec2mod._docker().ping()
    except Exception:
        pytest.skip("Docker is not available")
    # back the notebook instance with a tiny image (treated as a user override,
    # so ensure_notebook_image() won't build the real JupyterLab image).
    monkeypatch.setattr(smmod, "NOTEBOOK_IMAGE", "alpine:3")
    s = SageMakerService()
    yield s
    s.delete_domain("pytest")


def test_create_domain_provisions_s3_ec2_ebs_then_deletes(sm):
    sm.delete_domain("pytest")  # clean slate
    d = sm.create_domain("pytest")
    assert d["status"] == "CREATE_COMPLETE"
    assert d["artifactsBucket"] == "oblako-sagemaker-pytest"
    iid = d["instanceId"]
    assert iid and iid.startswith("i-")

    # S3 artifacts bucket is real
    buckets = {
        b["Name"] for b in S3ProxyService().get_client().list_buckets()["Buckets"]
    }
    assert "oblako-sagemaker-pytest" in buckets

    # EC2 notebook instance is a real container with the EBS volume mounted +
    # its JupyterLab port published
    c = ec2mod._docker().containers.get(ec2mod._container_name(iid))
    assert c.status == "running"
    assert ec2mod.EBS_MOUNT in {m["Destination"] for m in c.attrs["Mounts"]}
    assert "8888/tcp" in c.attrs["NetworkSettings"]["Ports"]

    sm.delete_domain("pytest")
    assert (
        ec2mod._docker().containers.list(
            all=True, filters={"name": ec2mod._container_name(iid)}
        )
        == []
    )
