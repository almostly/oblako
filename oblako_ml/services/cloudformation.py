"""CloudFormation service: local AWS CloudFormation over oblako's real engines.

Unlike the other services there is no container here — it's a pure in-process
server that, on ExecuteChangeSet, provisions resources into the engines that
*do* run in containers (S3Proxy, DynamoDB Local, moto). Point a boto3
``cloudformation`` client — or ``aws cloudformation deploy`` / ``sam deploy``
via AWS_ENDPOINT_URL_CLOUDFORMATION — at it and stacks land in oblako for real.
"""

from __future__ import annotations


class CloudFormationService:
    """Manage the in-process CloudFormation server and hand out boto3 clients."""

    name = "cloudformation"

    def __init__(self, port: int = 5601, region: str = "us-east-1"):
        """Initialize with the given port and AWS region."""
        self.port = port
        self.region = region

    @property
    def endpoint_url(self) -> str:
        """Return the HTTP endpoint URL for the local CloudFormation server."""
        return f"http://localhost:{self.port}"

    def start_server(self) -> str:
        """Start the CloudFormation server in-process (idempotent). Returns its URL."""
        from oblako_ml.cloudformation import start_in_thread

        return start_in_thread(port=self.port)

    def get_client(self, autostart: bool = True):
        """boto3 ``cloudformation`` client whose stacks provision into oblako."""
        import boto3
        from oblako_ml import cloudformation

        if autostart and not cloudformation.is_running(self.port):
            self.start_server()
        return boto3.client(
            "cloudformation",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )

    def is_running(self) -> bool:
        """Return True if the local CloudFormation server is already listening."""
        from oblako_ml import cloudformation

        return cloudformation.is_running(self.port)
