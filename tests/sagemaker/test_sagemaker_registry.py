"""SageMaker Model Registry: package groups, versioned packages, approval.

The registry is in-memory metadata (no Docker, no S3), so this drives unmodified
boto3 ``sagemaker`` against the in-thread engine and needs no external services.
It models the MLOps promotion flow: create a group, register model versions into
it, then approve/reject them.
"""

import pytest


@pytest.fixture(scope="module")
def sm():
    from oblako.services import SageMakerService

    return SageMakerService().get_client()


def _infspec():
    return {
        "InferenceSpecification": {
            "Containers": [{"Image": "oblako-sagemaker-serve:latest"}],
            "SupportedContentTypes": ["text/csv"],
            "SupportedResponseMIMETypes": ["text/csv"],
        }
    }


def test_model_registry_group_versions_and_approval(sm):
    group = "fraud-scorecard"
    sm.create_model_package_group(
        ModelPackageGroupName=group,
        ModelPackageGroupDescription="fraud models",
    )
    desc = sm.describe_model_package_group(ModelPackageGroupName=group)
    assert desc["ModelPackageGroupStatus"] == "Completed"
    assert group in [
        g["ModelPackageGroupName"]
        for g in sm.list_model_package_groups()["ModelPackageGroupSummaryList"]
    ]

    # register two versions into the group
    v1 = sm.create_model_package(ModelPackageGroupName=group, **_infspec())[
        "ModelPackageArn"
    ]
    v2 = sm.create_model_package(ModelPackageGroupName=group, **_infspec())[
        "ModelPackageArn"
    ]
    assert v1.endswith(f"{group}/1")
    assert v2.endswith(f"{group}/2")

    # versions default to pending approval
    d1 = sm.describe_model_package(ModelPackageName=v1)
    assert d1["ModelPackageVersion"] == 1
    assert d1["ModelApprovalStatus"] == "PendingManualApproval"

    packages = sm.list_model_packages(ModelPackageGroupName=group)[
        "ModelPackageSummaryList"
    ]
    assert {p["ModelPackageVersion"] for p in packages} == {1, 2}

    # approve v1, reject v2 (the promotion gate)
    sm.update_model_package(
        ModelPackageArn=v1,
        ModelApprovalStatus="Approved",
        ApprovalDescription="passed eval",
    )
    sm.update_model_package(ModelPackageArn=v2, ModelApprovalStatus="Rejected")
    assert sm.describe_model_package(ModelPackageName=v1)["ModelApprovalStatus"] == (
        "Approved"
    )
    assert sm.describe_model_package(ModelPackageName=v2)["ModelApprovalStatus"] == (
        "Rejected"
    )

    sm.delete_model_package(ModelPackageName=v2)
    with pytest.raises(sm.exceptions.ClientError):
        sm.describe_model_package(ModelPackageName=v2)

    sm.delete_model_package_group(ModelPackageGroupName=group)
    with pytest.raises(sm.exceptions.ClientError):
        sm.describe_model_package_group(ModelPackageGroupName=group)


def test_unversioned_model_package(sm):
    arn = sm.create_model_package(
        ModelPackageName="standalone-model",
        ModelApprovalStatus="Approved",
        **_infspec(),
    )["ModelPackageArn"]
    assert arn.endswith("model-package/standalone-model")
    pkg = sm.describe_model_package(ModelPackageName="standalone-model")
    assert "ModelPackageVersion" not in pkg  # unversioned: no version member
    assert pkg["ModelApprovalStatus"] == "Approved"
