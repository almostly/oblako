"""AppConfig service: local AWS AppConfig (control + data plane) + the agent.

In-process server (no container) — started lazily on first get_client(), like
CloudFormation and the Glue Data Catalog. Hands out:
  - get_client():      boto3 ``appconfig`` (management plane)
  - get_data_client(): boto3 ``appconfigdata`` (runtime: sessions + GetLatestConfiguration)
  - get_agent():       the bundled AppConfigClient (feature-flag rule evaluation)
"""

from __future__ import annotations

from oblako import config, ports
from . import boto


class AppConfigService:
    """Local AppConfig + AppConfigData, served by the in-process engine."""

    name = "appconfig"

    def __init__(self, port: int = ports.APPCONFIG):
        """Initialize the accessor for the in-process AppConfig server."""
        self.port = port

    @property
    def endpoint_url(self) -> str:
        """Endpoint URL boto3 clients connect to."""
        return f"http://localhost:{self.port}"

    def start_server(self) -> str:
        """Start the in-process AppConfig server (idempotent). Returns its URL."""
        from oblako.engines.appconfig import start_in_thread

        return start_in_thread(port=self.port)

    def _ensure(self, autostart: bool) -> None:
        from oblako.engines import appconfig

        if autostart and not appconfig.is_running(self.port):
            self.start_server()

    def get_client(self, autostart: bool = True):
        """boto3 ``appconfig`` management client (applications, profiles, versions…)."""
        self._ensure(autostart)
        return boto.client("appconfig", self.endpoint_url)

    def get_data_client(self, autostart: bool = True):
        """boto3 ``appconfigdata`` client (StartConfigurationSession / GetLatestConfiguration)."""
        self._ensure(autostart)
        return boto.client("appconfigdata", self.endpoint_url)

    def get_agent(self, autostart: bool = True):
        """Return the bundled AppConfigClient agent, wired to oblako, for flag evaluation."""
        self._ensure(autostart)
        from oblako.engines.appconfig import AppConfigClient

        return AppConfigClient(
            endpoint_url=self.endpoint_url, region=config.region(),
            aws_access_key_id="test", aws_secret_access_key="test",
        )
