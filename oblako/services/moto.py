"""Moto service: backs the Redshift (and other) AWS control-plane APIs.

Runs the official `motoserver/moto` image. boto3 clients pointed at it via
`endpoint_url` get real AWS control-plane behavior (e.g. Redshift clusters,
nodes, endpoints) without touching the cloud.
"""

from oblako import ports
import urllib.request

from .base import Service, PortMapping


class MotoService(Service):
    """Moto server service providing AWS control-plane APIs locally."""

    def __init__(self, host_port: int = ports.MOTO):
        """Initialize the Moto service on the given host port.

        Mounts /var/run/docker.sock so moto's Lambda backend can spawn real
        function containers (lambci/lambda images) on the host Docker daemon
        when boto3 calls lambda:invoke. Without this, invoke fails with
        "error running docker: No such file or directory".
        """
        super().__init__(
            name="moto",
            image="motoserver/moto:latest",
            ports=[PortMapping(container_port=5000, host_port=host_port)],
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}
            },
            environment={
                # Function containers must be able to reach moto over the host
                # Docker network — host.docker.internal works on Docker Desktop.
                "MOTO_DOCKER_LAMBDA_INVOKE_HOST": "host.docker.internal",
                # AWS's managed policies (AWSLambdaBasicExecutionRole, ...) exist in
                # every account; SAM and CloudFormation roles attach them
                "MOTO_IAM_LOAD_MANAGED_POLICIES": "true",
                # NOTE: don't set MOTO_DOCKER_LAMBDA_IMAGE — the official AWS
                # Lambda images use RIE (HTTP server entrypoint) and are not
                # compatible with moto's one-shot CLI invoke. moto falls back to
                # ghcr.io/shogo82148/lambda-{lang}:{ver}, which uses a moto-
                # compatible entrypoint. Pick runtimes whose shogo82148 image
                # uses AL2023 (glibc 2.34) — currently python3.12 — so modern
                # pandas/numpy wheels load. python3.11 is still on AL2 (glibc 2.26).
            },
        )
        self.host_port = host_port

    def start(self) -> None:
        """Start moto, then the EventBridge proxy in front of it.

        moto serves EventBridge's API and delivers PutEvents to SQS, SNS and
        Lambda, but it never fires scheduled rules or runs Redshift Data targets
        (the scheduled-query path); the proxy adds both. It is oblako's
        EventBridge endpoint, so a rule created through it runs.
        """
        from oblako.engines import host

        super().start()
        if self.host_port == ports.MOTO:
            host.start("eventbridge")

    @property
    def endpoint_url(self) -> str:
        """Return the Moto server endpoint URL."""
        return f"http://localhost:{self.host_port}"

    def _health_check(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"{self.endpoint_url}/moto-api/", timeout=2
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
