"""Account-free local stubs for the SageMaker SDK's own local modes.

The SDK's client-side local modes -- ``ModelTrainer`` and ``ModelBuilder``
(``Mode.LOCAL_CONTAINER`` / ``Mode.IN_PROCESS``), ``LocalPipelineSession`` -- run
the model on this machine but still reach for AWS at the edges: they validate the
execution role through STS/IAM, look up a default ``sagemaker-<region>-<account>``
bucket, and pull the image from ECR. With no account (and oblako's dummy creds)
those calls fail.

``use_local_stubs()`` neutralizes exactly those three, and lets local jobs read an
``S3Prefix`` input under ``response_checksum_validation = when_required`` (the SDK
mistakes the folder for a missing object there), so the SDK's local modes run
fully locally against oblako -- pair it with ``AWS_ENDPOINT_URL_S3`` pointing boto3
at S3Proxy and a locally built image. The role check is patched for both
``ModelBuilder`` and ``ModelTrainer``, and an explicit ``default_bucket=`` on a
session is kept (``"local"`` only when none is given). This is a *client-side*
helper (it patches the installed SageMaker SDK); it's a no-op if the relevant
modules aren't importable.

    from oblako.engines.sagemaker import use_local_stubs
    use_local_stubs()   # then build/deploy with ModelBuilder(mode=Mode.IN_PROCESS), etc.
"""

from __future__ import annotations


def use_local_stubs() -> None:
    """Patch the SDK's role validation, default-bucket lookup, image pull and S3 folder download.

    Safe to call more than once. Silently skips any piece the installed SDK
    doesn't expose (module layouts differ across v3 minor versions).
    """
    import contextlib

    def _keep_role(provided_role=None, **_):
        """Return the provided role without validating it."""
        return provided_role

    # 1. keep the provided role as-is instead of validating it through STS/IAM.
    # ModelBuilder and ModelTrainer each import resolve_and_validate_role into
    # their own module, so both names need the patch.
    with contextlib.suppress(Exception):
        from sagemaker.serve import model_builder

        setattr(model_builder, "resolve_and_validate_role", _keep_role)
    with contextlib.suppress(Exception):
        from sagemaker.train import defaults

        setattr(defaults, "resolve_and_validate_role", _keep_role)

    # 2. keep an explicit Session(default_bucket=...) and fall back to "local",
    # instead of resolving sagemaker-<region>-<account> through STS
    with contextlib.suppress(Exception):
        from sagemaker.core.helper.session_helper import Session

        def _local_bucket(self):
            """Return the explicit default bucket, or ``local``."""
            return getattr(self, "_default_bucket_name_override", None) or "local"

        Session.default_bucket = _local_bucket

    # 3. connect to Docker but don't pull the (locally built) image from ECR
    with contextlib.suppress(Exception):
        from sagemaker.serve.mode import local_container_mode

        def _use_local_image(self, image):
            """Connect to Docker without pulling the image."""
            self.client = local_container_mode._get_docker_client()
            self.client.ping()

        local_container_mode.LocalContainerMode._pull_image = _use_local_image

    # 4. download an S3Prefix input whose prefix is not an object. The SDK first
    # tries the prefix as one object and takes only HeadObject's 404 to mean "a
    # folder"; with response_checksum_validation = when_required (oblako's
    # profile) s3transfer skips the HEAD, so the miss is GetObject's NoSuchKey
    # and the step fails. Treat NoSuchKey the same way.
    with contextlib.suppress(Exception):
        import functools
        import importlib

        from sagemaker.core import common_utils

        original = getattr(
            common_utils.download_folder, "__wrapped__", common_utils.download_folder
        )

        @functools.wraps(original)
        def _download_folder(bucket_name, prefix, target, sagemaker_session):
            """Download an S3 folder, treating NoSuchKey on the prefix as a folder."""
            from botocore.exceptions import ClientError

            try:
                return original(bucket_name, prefix, target, sagemaker_session)
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "NoSuchKey":
                    raise
            owner = sagemaker_session._get_account_id_if_default_bucket(bucket_name)
            common_utils._download_files_under_prefix(
                bucket_name,
                prefix.lstrip("/"),
                target,
                sagemaker_session.s3_resource,
                extra_args={"ExpectedBucketOwner": owner} if owner else None,
            )

        for name in (
            "sagemaker.core.common_utils",
            "sagemaker.core.utils",
            "sagemaker.utils",
            "sagemaker.core.modules.local_core.local_container",
            "sagemaker.train.local.local_container",
        ):
            with contextlib.suppress(Exception):
                module = importlib.import_module(name)
                if hasattr(module, "download_folder"):
                    setattr(module, "download_folder", _download_folder)
