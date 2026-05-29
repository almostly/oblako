"""Bedrock service: local Amazon Bedrock, powered by Ollama.

The container is the Ollama engine (`ollama/ollama`), but oblako surfaces it as
Bedrock. Two ways in:
  * engine helpers - ``pull_model`` / ``list_models`` (Ollama model management).
  * ``get_client()`` - boto3 ``bedrock-runtime`` client whose ``invoke_model`` /
                       ``converse`` calls are translated to Ollama (auto-starts
                       the local bedrock-runtime server).
"""

from oblako import ports
import httpx

from oblako import config
from .base import Service, PortMapping


class BedrockService(Service):
    """Local Amazon Bedrock service powered by Ollama."""

    def __init__(
        self,
        host_port: int = ports.OLLAMA,
        runtime_port: int = ports.BEDROCK_RUNTIME,
        region: str | None = None,
    ):
        """Initialize the Bedrock service with Ollama engine and runtime port."""
        super().__init__(
            name="bedrock",
            image="ollama/ollama:latest",
            ports=[PortMapping(container_port=11434, host_port=host_port)],
            volumes={"oblako-ollama-data": {"bind": "/root/.ollama", "mode": "rw"}},
        )
        self.host_port = host_port
        self.runtime_port = runtime_port
        self.region = region or config.region()

    @property
    def url(self) -> str:
        """Ollama engine URL."""
        return f"http://localhost:{self.host_port}"

    # -------------------------------------------------------------------------------
    # Engine (Ollama) model management
    # -------------------------------------------------------------------------------
    def pull_model(self, model: str | None = None) -> None:
        """Pull a model into the engine (defaults to the small qwen default)."""
        from oblako.engines.bedrock.models import DEFAULT_MODEL

        model = model or DEFAULT_MODEL
        container = self.client.containers.get(self.container_name)
        exit_code, output = container.exec_run(f"ollama pull {model}", stream=False)
        print(output.decode("utf-8"))

    def list_models(self) -> list[str]:
        """List locally available models."""
        resp = httpx.get(f"{self.url}/api/tags", timeout=10.0)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]

    # -------------------------------------------------------------------------------
    # boto3 bedrock-runtime
    # -------------------------------------------------------------------------------
    def start_runtime_server(self):
        """Start the local bedrock-runtime server in-process (idempotent)."""
        from oblako.engines.bedrock_runtime import start_in_thread

        return start_in_thread(port=self.runtime_port, ollama_url=self.url)

    def _boto_client(self, service: str, autostart: bool):
        from oblako.engines import bedrock_runtime
        from . import boto

        if autostart and not bedrock_runtime.is_running(self.runtime_port):
            self.start_runtime_server()
        return boto.client(
            service,
            f"http://localhost:{self.runtime_port}",
            region=self.region,
        )

    def get_client(self, autostart: bool = True):
        """boto3 ``bedrock-runtime`` client backed by Ollama via BedrockAdapter."""
        return self._boto_client("bedrock-runtime", autostart)

    def get_control_client(self, autostart: bool = True):
        """boto3 ``bedrock`` control-plane client (foundation models, batch jobs)."""
        return self._boto_client("bedrock", autostart)

    def _health_check(self) -> bool:
        try:
            resp = httpx.get(f"{self.url}/api/tags", timeout=3.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            # a starting service may accept then reset the connection
            # (httpx.ReadError), not just refuse it — any transport error = not ready
            return False


# Backwards-compatible alias: the engine is still Ollama under the hood.
OllamaService = BedrockService
