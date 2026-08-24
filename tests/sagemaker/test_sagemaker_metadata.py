"""Control-plane metadata ops: model/endpoint-config CRUD, tags, Studio domains.

These operations are pure in-memory bookkeeping (no Docker, no S3), so this test
drives unmodified boto3 ``sagemaker`` against the in-thread engine and needs no
external services. It mirrors the resource CRUD surface of the LocalStack Pro
SageMaker provider.
"""

import pytest


@pytest.fixture(scope="module")
def sm():
    from oblako.services import SageMakerService

    return SageMakerService().get_client()


def _arn_role():
    return "arn:aws:iam::000000000000:role/oblako-execution"


def test_model_crud(sm):
    sm.create_model(
        ModelName="md-crud",
        PrimaryContainer={"Image": "img:latest"},
        ExecutionRoleArn=_arn_role(),
    )
    desc = sm.describe_model(ModelName="md-crud")
    assert desc["ModelName"] == "md-crud"
    assert desc["PrimaryContainer"]["Image"] == "img:latest"
    assert "md-crud" in [m["ModelName"] for m in sm.list_models()["Models"]]
    sm.delete_model(ModelName="md-crud")
    with pytest.raises(sm.exceptions.ClientError):
        sm.describe_model(ModelName="md-crud")


def test_endpoint_config_crud_and_list_endpoints(sm):
    sm.create_endpoint_config(
        EndpointConfigName="ec-crud",
        ProductionVariants=[
            {
                "VariantName": "v0",
                "ModelName": "some-model",
                "InitialInstanceCount": 1,
                "InstanceType": "ml.m5.large",
            }
        ],
    )
    desc = sm.describe_endpoint_config(EndpointConfigName="ec-crud")
    assert desc["ProductionVariants"][0]["VariantName"] == "v0"
    names = [c["EndpointConfigName"] for c in sm.list_endpoint_configs()["EndpointConfigs"]]
    assert "ec-crud" in names
    sm.delete_endpoint_config(EndpointConfigName="ec-crud")
    with pytest.raises(sm.exceptions.ClientError):
        sm.describe_endpoint_config(EndpointConfigName="ec-crud")
    # no endpoints were started, but the op is available and well-formed
    assert isinstance(sm.list_endpoints()["Endpoints"], list)


def test_tags_add_list_delete(sm):
    sm.create_model(
        ModelName="md-tags",
        PrimaryContainer={"Image": "img:latest"},
        ExecutionRoleArn=_arn_role(),
    )
    arn = sm.describe_model(ModelName="md-tags")["ModelArn"]
    sm.add_tags(ResourceArn=arn, Tags=[{"Key": "team", "Value": "ml"}])
    sm.add_tags(
        ResourceArn=arn,
        Tags=[{"Key": "env", "Value": "local"}, {"Key": "team", "Value": "mlops"}],
    )
    tags = {t["Key"]: t["Value"] for t in sm.list_tags(ResourceArn=arn)["Tags"]}
    assert tags == {"team": "mlops", "env": "local"}  # team overwritten
    sm.delete_tags(ResourceArn=arn, TagKeys=["env"])
    tags = {t["Key"]: t["Value"] for t in sm.list_tags(ResourceArn=arn)["Tags"]}
    assert tags == {"team": "mlops"}


def test_stop_unknown_job_is_not_found(sm):
    with pytest.raises(sm.exceptions.ClientError):
        sm.stop_training_job(TrainingJobName="does-not-exist")


def test_studio_domain_and_user_profile_lifecycle(sm):
    created = sm.create_domain(
        DomainName="studio-1",
        AuthMode="IAM",
        DefaultUserSettings={"ExecutionRole": _arn_role()},
    )
    domain_id = created["DomainId"]
    assert domain_id.startswith("d-")
    assert created["Url"].endswith(".sagemaker.aws")

    desc = sm.describe_domain(DomainId=domain_id)
    assert desc["DomainName"] == "studio-1"
    assert desc["Status"] == "InService"

    sm.update_domain(
        DomainId=domain_id,
        DefaultUserSettings={"ExecutionRole": _arn_role(), "JupyterServerAppSettings": {}},
    )
    assert domain_id in [d["DomainId"] for d in sm.list_domains()["Domains"]]

    sm.create_user_profile(DomainId=domain_id, UserProfileName="alice")
    prof = sm.describe_user_profile(DomainId=domain_id, UserProfileName="alice")
    assert prof["UserProfileName"] == "alice"
    sm.update_user_profile(
        DomainId=domain_id,
        UserProfileName="alice",
        UserSettings={"ExecutionRole": _arn_role()},
    )
    listed = sm.list_user_profiles(DomainIdEquals=domain_id)["UserProfiles"]
    assert [p["UserProfileName"] for p in listed] == ["alice"]

    sm.delete_user_profile(DomainId=domain_id, UserProfileName="alice")
    with pytest.raises(sm.exceptions.ClientError):
        sm.describe_user_profile(DomainId=domain_id, UserProfileName="alice")

    sm.delete_domain(DomainId=domain_id)
    with pytest.raises(sm.exceptions.ClientError):
        sm.describe_domain(DomainId=domain_id)
