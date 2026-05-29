"""CloudFormation service: local AWS CloudFormation over oblako's real engines.

Unlike the other services there is no container here — it's a pure in-process
server that, on ExecuteChangeSet, provisions resources into the engines that
*do* run in containers (S3Proxy, DynamoDB Local, moto). Point a boto3
``cloudformation`` client — or ``aws cloudformation deploy`` / ``sam deploy``
via AWS_ENDPOINT_URL_CLOUDFORMATION — at it and stacks land in oblako for real.
"""

from __future__ import annotations

from oblako import ports
from oblako import config


class CloudFormationService:
    """Manage the in-process CloudFormation server and hand out boto3 clients."""

    name = "cloudformation"

    def __init__(self, port: int = ports.CLOUDFORMATION, region: str | None = None):
        """Initialize with the given port and AWS region."""
        self.port = port
        self.region = region or config.region()

    @property
    def endpoint_url(self) -> str:
        """Return the HTTP endpoint URL for the local CloudFormation server."""
        return f"http://localhost:{self.port}"

    def start_server(self) -> str:
        """Start the CloudFormation server in-process (idempotent). Returns its URL."""
        from oblako.engines.cloudformation import start_in_thread

        return start_in_thread(port=self.port)

    def get_client(self, autostart: bool = True):
        """boto3 ``cloudformation`` client whose stacks provision into oblako."""
        from oblako.engines import cloudformation
        from . import boto

        if autostart and not cloudformation.is_running(self.port):
            self.start_server()
        return boto.client("cloudformation", self.endpoint_url, region=self.region)

    def is_running(self) -> bool:
        """Return True if the local CloudFormation server is already listening."""
        from oblako.engines import cloudformation

        return cloudformation.is_running(self.port)
