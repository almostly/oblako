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
