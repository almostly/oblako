"""Caddy reverse proxy — vanity AWS-style hostnames in front of oblako services.

Gives the MLflow App (and future services) "real-looking" URLs like
``http://mlflow.oblako.aws`` instead of ``http://localhost:5050``. Caddy runs as
a managed container; the Caddyfile is generated from a small route table.

One-time per machine: add ``127.0.0.1 mlflow.oblako.aws`` to ``/etc/hosts``
(``oblako.vanity_hosts_line()`` prints exactly what to paste).
"""

from __future__ import annotations

from oblako import ports
from pathlib import Path

import httpx

from oblako import config

from .base import Service, PortMapping

CADDY_DIR = Path.home() / ".oblako" / "caddy"


def vanity_host(service: str) -> str:
    """Return the AWS-shaped vanity hostname for a service.

    Mirrors how SageMaker-managed MLflow tracking servers (and similar AWS
    consoles) look: ``<service>-oblako.<account>.<region>.experiments.sagemaker.aws``.
    """
    return f"{service}-oblako.{config.account_id()}.{config.region()}.experiments.sagemaker.aws"


def vanity_routes() -> dict[str, str]:
    """Build the Caddy route table from the configured account/region."""
    # Caddy reaches upstream services on the host via host.docker.internal
    # (the container has the host-gateway alias).
    return {
        vanity_host("mlflow"): "host.docker.internal:5050",
    }


def _caddyfile(routes: dict[str, str]) -> str:
    lines = ["{", "    auto_https off", "    admin off", "}", ""]
    for host, upstream in routes.items():
        # Rewrite the upstream Host header to localhost so MLflow 3's DNS-rebinding
        # protection accepts the request (it would otherwise reject the AWS-shaped
        # hostname). Per-route override keeps the public hostname AWS-shaped.
        lines += [
            f"http://{host} {{",
            f"    reverse_proxy {upstream} {{",
            "        header_up Host localhost",
            "    }",
            "}",
            "",
        ]
    return "\n".join(lines)


class CaddyService(Service):
    """Caddy reverse proxy fronting oblako services with AWS-style hostnames."""

    name = "caddy"

    def __init__(self, host_port: int = ports.CADDY, routes: dict[str, str] | None = None):
        """Initialize on host_port (80 by default; pick a higher one if :80 is busy)."""
        CADDY_DIR.mkdir(parents=True, exist_ok=True)
        self.routes = routes if routes is not None else vanity_routes()
        (CADDY_DIR / "Caddyfile").write_text(_caddyfile(self.routes))
        super().__init__(
            name="caddy",
            image="caddy:alpine",
            ports=[PortMapping(container_port=80, host_port=host_port)],
            volumes={str(CADDY_DIR): {"bind": "/etc/caddy", "mode": "ro"}},
            # Reach the upstream services (MLflow etc.) on the host.
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
        self.host_port = host_port

    def vanity_url(self, host: str) -> str:
        """Return the user-facing URL for a registered vanity host."""
        suffix = "" if self.host_port == 80 else f":{self.host_port}"
        return f"http://{host}{suffix}"

    def hosts_line(self) -> str:
        """Return the single /etc/hosts line that makes the vanity hosts resolve."""
        return "127.0.0.1 " + " ".join(self.routes.keys())

    def _health_check(self) -> bool:
        try:
            # Caddy without a default site returns 404 on /, but the port is open.
            resp = httpx.get(f"http://localhost:{self.host_port}/", timeout=3.0)
            return resp.status_code < 500
        except httpx.HTTPError:  # any transport error (incl. accept-then-reset) = not ready
            return False
