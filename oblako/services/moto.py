"""Moto service: backs the Redshift (and other) AWS control-plane APIs.

Runs the official `motoserver/moto` image. boto3 clients pointed at it via
`endpoint_url` get real AWS control-plane behavior (e.g. Redshift clusters,
nodes, endpoints) without touching the cloud.
"""

import urllib.request

from .base import Service, PortMapping


class MotoService(Service):
    """Moto server service providing AWS control-plane APIs locally."""

    def __init__(self, host_port: int = 5500):
        """Initialize the Moto service on the given host port."""
        super().__init__(
            name="moto",
            image="motoserver/moto:latest",
            ports=[PortMapping(container_port=5000, host_port=host_port)],
        )
        self.host_port = host_port

    @property
    def endpoint_url(self) -> str:
        """Return the Moto server endpoint URL."""
        return f"http://localhost:{self.host_port}"

    def _health_check(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.endpoint_url}/moto-api/", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False
