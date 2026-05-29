"""Step Functions Local service."""

from oblako import ports
import json
from pathlib import Path

import httpx

from oblako import config
from .base import Service, PortMapping
from .boto import BotoService

DUMMY_ROLE = config.arn("iam", "role/DummyRole", region_scoped=False)

# Mounted into the container so executions can run the bundled ML templates in
# SFN Local mock mode (canned task results). The file is hot-reloaded on each run.
MOCK_CONFIG_MOUNT = "/oblako/sfn"


@BotoService("stepfunctions")
class StepFunctionsService(Service):
    """Step Functions Local service backed by the official Amazon image."""

    def __init__(
        self,
        host_port: int = ports.STEPFUNCTIONS,
        lambda_endpoint: str = "http://host.docker.internal:3001",
    ):
        """Initialize the Step Functions service with the given host port and Lambda endpoint."""
        mock_dir = Path.home() / ".oblako" / "sfn"
        super().__init__(
            name="stepfunctions",
            image="amazon/aws-stepfunctions-local:latest",
            ports=[PortMapping(container_port=8083, host_port=host_port)],
            environment={
                "LAMBDA_ENDPOINT": lambda_endpoint,
                "SFN_MOCK_CONFIG": f"{MOCK_CONFIG_MOUNT}/MockConfigFile.json",
            },
            volumes={str(mock_dir): {"bind": MOCK_CONFIG_MOUNT, "mode": "ro"}},
            # Reach the host lambda shim (oblako.lambda_shim on :3001) for live
            # lambda:invoke tasks — e.g. the Bedrock prompt-chain -> local model.
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
        self.host_port = host_port
        self.mock_config_path = mock_dir / "MockConfigFile.json"

    def write_mock_config(self, config: dict) -> None:
        """Write the SFN Local mock configuration to the mounted file (hot-reloaded)."""
        self.mock_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.mock_config_path.write_text(json.dumps(config, indent=2))

    def start(self) -> None:
        """Seed the mock config from the bundled ML templates, then start the container."""
        from . import sfn_templates

        self.write_mock_config(sfn_templates.build_mock_config())
        super().start()

    @property
    def endpoint_url(self) -> str:
        """Return the Step Functions Local endpoint URL."""
        return f"http://localhost:{self.host_port}"

    def create_state_machine(
        self, name: str, definition: dict, role_arn: str = DUMMY_ROLE
    ) -> str:
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
                headers={
                    "Content-Type": "application/x-amz-json-1.0",
                    "X-Amz-Target": "AWSStepFunctions.ListStateMachines",
                },
                content="{}",
                timeout=3.0,
            )
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError):
            return False
