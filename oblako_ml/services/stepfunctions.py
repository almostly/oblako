"""Step Functions Local service."""

import json

import boto3
import httpx

from .base import Service, PortMapping

DUMMY_ROLE = "arn:aws:iam::012345678901:role/DummyRole"


class StepFunctionsService(Service):
    """Step Functions Local service backed by the official Amazon image."""

    def __init__(self, host_port: int = 8083, lambda_endpoint: str = "http://host.docker.internal:3001"):
        """Initialize the Step Functions service with the given host port and Lambda endpoint."""
        super().__init__(
            name="stepfunctions",
            image="amazon/aws-stepfunctions-local:latest",
            ports=[PortMapping(container_port=8083, host_port=host_port)],
            environment={
                "LAMBDA_ENDPOINT": lambda_endpoint,
            },
        )
        self.host_port = host_port

    @property
    def endpoint_url(self) -> str:
        """Return the Step Functions Local endpoint URL."""
        return f"http://localhost:{self.host_port}"

    def get_client(self):
        """Return a boto3 Step Functions client."""
        return boto3.client(
            "stepfunctions",
            endpoint_url=self.endpoint_url,
            aws_access_key_id="test",
            aws_secret_access_key="test",
            region_name="us-east-1",
        )

    def create_state_machine(self, name: str, definition: dict, role_arn: str = DUMMY_ROLE) -> str:
        """Create a state machine and return its ARN."""
        sfn = self.get_client()
        resp = sfn.create_state_machine(
            name=name,
            definition=json.dumps(definition),
            roleArn=role_arn,
        )
        return resp["stateMachineArn"]

    def execute(self, state_machine_arn: str, input_data: dict) -> str:
        """Start an execution and return the execution ARN."""
        sfn = self.get_client()
        resp = sfn.start_execution(
            stateMachineArn=state_machine_arn,
            input=json.dumps(input_data),
        )
        return resp["executionArn"]

    def _health_check(self) -> bool:
        try:
            resp = httpx.post(
                self.endpoint_url,
                headers={"Content-Type": "application/x-amz-json-1.0", "X-Amz-Target": "AWSStepFunctions.ListStateMachines"},
                content="{}",
                timeout=3.0,
            )
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError):
            return False
