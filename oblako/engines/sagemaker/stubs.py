"""Account-free local stubs for the SageMaker SDK's own local modes.

The SDK's client-side local modes -- ``ModelBuilder`` (``Mode.LOCAL_CONTAINER`` /
``Mode.IN_PROCESS``), ``LocalPipelineSession`` -- run the model on this machine but
still reach for AWS at the edges: they validate the execution role through STS/IAM,
look up a default ``sagemaker-<region>-<account>`` bucket, and pull the image from
ECR. With no account (and oblako's dummy creds) those calls fail.

``use_local_stubs()`` neutralizes exactly those three, so the SDK's local modes run
fully locally against oblako -- pair it with ``AWS_ENDPOINT_URL_S3`` pointing boto3
at S3Proxy and a locally built image. This is a *client-side* helper (it patches the
installed SageMaker SDK); it's a no-op if the relevant modules aren't importable.

    from oblako.engines.sagemaker import use_local_stubs
    use_local_stubs()   # then build/deploy with ModelBuilder(mode=Mode.IN_PROCESS), etc.
"""

from __future__ import annotations


def use_local_stubs() -> None:
    """Patch the SDK's role validation, default-bucket lookup, and image pull.

    Safe to call more than once. Silently skips any piece the installed SDK
    doesn't expose (module layouts differ across v3 minor versions).
    """
    import contextlib

    # 1. keep the provided role as-is instead of validating it through STS/IAM
    with contextlib.suppress(Exception):
        from sagemaker.serve import model_builder

        def _keep_role(provided_role=None, **_):
            return provided_role

        model_builder.resolve_and_validate_role = _keep_role

    # 2. return a local default bucket instead of resolving sagemaker-<region>-<account>
    with contextlib.suppress(Exception):
        from sagemaker.core.helper.session_helper import Session

        Session.default_bucket = lambda _self: "local"

    # 3. connect to Docker but don't pull the (locally built) image from ECR
    with contextlib.suppress(Exception):
        from sagemaker.serve.mode import local_container_mode

        def _use_local_image(self, image):
            self.client = local_container_mode._get_docker_client()
            self.client.ping()

        local_container_mode.LocalContainerMode._pull_image = _use_local_image
