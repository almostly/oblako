"""Account-free client stubs for the SageMaker SDK's local modes."""

import pytest

from oblako.engines.sagemaker import use_local_stubs


def test_use_local_stubs_is_a_safe_noop_without_the_sdk():
    # every patch is suppressed if the SDK isn't importable, so it never raises;
    # and it's safe to apply more than once.
    use_local_stubs()
    use_local_stubs()


def test_patches_role_validation_when_the_sdk_is_present():
    model_builder = pytest.importorskip("sagemaker.serve.model_builder")
    use_local_stubs()
    # role validation is neutralized: the provided role is returned as-is (no STS)
    assert model_builder.resolve_and_validate_role("arn:aws:iam::0:role/x") == (
        "arn:aws:iam::0:role/x"
    )


def test_patches_model_trainer_role_validation():
    # ModelTrainer imports resolve_and_validate_role into sagemaker.train.defaults,
    # so patching model_builder alone leaves training validating through IAM
    defaults = pytest.importorskip("sagemaker.train.defaults")
    use_local_stubs()
    assert defaults.resolve_and_validate_role("arn:aws:iam::0:role/x") == (
        "arn:aws:iam::0:role/x"
    )


def test_default_bucket_keeps_an_explicit_bucket():
    session_helper = pytest.importorskip("sagemaker.core.helper.session_helper")
    use_local_stubs()
    session = session_helper.Session.__new__(session_helper.Session)
    session._default_bucket_name_override = None
    assert session.default_bucket() == "local"
    session._default_bucket_name_override = "my-pipeline-bucket"
    assert session.default_bucket() == "my-pipeline-bucket"
