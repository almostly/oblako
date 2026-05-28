"""Lambda service: AWS Lambda control plane + real Docker-based invocation.

Lambda is one of moto's most feature-complete services — it stores function
metadata, layers, versions, aliases, event source mappings, and (when given
access to the Docker socket) actually *executes* the function in a real
lambci/lambda container. We mount /var/run/docker.sock into the moto container
in `MotoService`, so `lambda:invoke` runs the handler for real.

This wrapper is just the boto3 entry point — there's no separate container.
Same pattern as IamService(moto=...) — moto owns the state, oblako owns the
ergonomics.
"""

from __future__ import annotations

import boto3

from oblako import config
from .moto import MotoService

DEFAULT_EXEC_ROLE_NAME = "oblako-lambda-exec"
ASSUME_ROLE_POLICY = (
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
    '"Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
)


class LambdaService:
    """AWS Lambda — control plane + invocation via moto + Docker."""

    def __init__(self, moto: MotoService):
        """Wire to the shared moto endpoint (no own container)."""
        self.moto = moto

    @property
    def endpoint_url(self) -> str:
        """Moto serves Lambda at the same endpoint as every other AWS API."""
        return self.moto.endpoint_url

    def get_client(self):
        """boto3 Lambda client pointed at moto."""
        return boto3.client(
            "lambda",
            endpoint_url=self.endpoint_url,
            aws_access_key_id="test",
            aws_secret_access_key="test",
            region_name=config.region(),
        )

    def get_iam_client(self):
        """boto3 IAM client pointed at moto (for the exec role)."""
        return boto3.client(
            "iam",
            endpoint_url=self.endpoint_url,
            aws_access_key_id="test",
            aws_secret_access_key="test",
            region_name=config.region(),
        )

    def ensure_exec_role(self, name: str = DEFAULT_EXEC_ROLE_NAME) -> str:
        """Create (idempotent) and return the ARN of the default Lambda exec role."""
        iam = self.get_iam_client()
        try:
            return iam.create_role(
                RoleName=name, AssumeRolePolicyDocument=ASSUME_ROLE_POLICY,
            )["Role"]["Arn"]
        except iam.exceptions.EntityAlreadyExistsException:
            return iam.get_role(RoleName=name)["Role"]["Arn"]

    def ensure_runtime_image(self, runtime: str, architecture: str = "x86_64") -> None:
        """Pre-pull the runtime image for a given architecture (linux/amd64 or linux/arm64).

        Real AWS Lambda defaults to x86_64. On Apple Silicon, Docker would pull the
        native arm64 manifest, which breaks layers built with x86_64 wheels (numpy,
        pandas, etc.). We pull the platform we want explicitly — moto then picks up
        the local image tag without caring about its arch.

        Uses the official AWS Lambda images (Amazon Linux 2023, glibc 2.34) so
        modern wheels work — matches what `MOTO_DOCKER_LAMBDA_IMAGE` is set to in
        the moto container.
        """
        import re
        import docker
        m = re.match(r"([a-z]+)([\d.]+)", runtime)
        if not m:
            return
        language, version = m.group(1), m.group(2)
        # Match moto's image lookup: ghcr.io/shogo82148/lambda-{lang}:{ver}.
        # Note: shogo82148's python3.11 image is on Amazon Linux 2 (glibc 2.26),
        # which is too old for modern pandas/numpy wheels. python3.12 is on
        # Amazon Linux 2023 (glibc 2.34) — pick that as the dashboard default.
        image = f"ghcr.io/shogo82148/lambda-{language}:{version}"
        platform = f"linux/{'amd64' if architecture == 'x86_64' else 'arm64'}"
        client = docker.from_env()
        wanted_arch = "amd64" if architecture == "x86_64" else "arm64"
        # If the cached image is the wrong arch, drop it so the pull is honest.
        try:
            local = client.images.get(image)
            if local.attrs.get("Architecture") not in (wanted_arch, None):
                client.images.remove(image, force=True)
        except docker.errors.ImageNotFound:
            pass
        client.images.pull(image, platform=platform)

    def start(self) -> None:
        """Bring moto up if it isn't already (no own container)."""
        self.moto.wait_ready(timeout=2) or self.moto.start()

    def stop(self) -> None:
        """No-op — moto owns the lifecycle."""

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Defer readiness to moto."""
        return self.moto.wait_ready(timeout=timeout)

    def status(self) -> str:
        """Defer status to moto."""
        return self.moto.status()
