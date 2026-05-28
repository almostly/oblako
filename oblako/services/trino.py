"""Local Trino — Athena-style SQL over the Iceberg REST catalog (= S3 Tables).

AWS Athena runs Trino/Presto-derived engines over S3. We expose the same
experience here: a Trino container with the Iceberg connector pre-wired to
oblako's Iceberg REST catalog and S3Proxy. ``SELECT * FROM iceberg.credit.applicants``
just works.

The catalog config is generated at ``~/.oblako/trino/catalog/iceberg.properties``
and mounted into the container.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from .base import Service, PortMapping

TRINO_CATALOG_DIR = Path.home() / ".oblako" / "trino" / "catalog"

_ICEBERG_PROPERTIES = """\
connector.name=iceberg
iceberg.catalog.type=rest
iceberg.rest-catalog.uri=http://host.docker.internal:8181
iceberg.rest-catalog.warehouse=s3://oblako-iceberg/
iceberg.rest-catalog.security=NONE
fs.native-s3.enabled=true
s3.endpoint=http://host.docker.internal:9000
s3.region=us-east-1
s3.path-style-access=true
s3.aws-access-key=test
s3.aws-secret-key=test
"""


class TrinoService(Service):
    """Local Trino, pre-wired with the Iceberg connector to oblako's catalog + S3."""

    def __init__(self, host_port: int = 8485):
        """Initialize on host_port (8485; Trino's internal port is 8080)."""
        TRINO_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
        (TRINO_CATALOG_DIR / "iceberg.properties").write_text(_ICEBERG_PROPERTIES)
        super().__init__(
            name="trino",
            image="trinodb/trino:latest",
            ports=[PortMapping(container_port=8080, host_port=host_port)],
            volumes={str(TRINO_CATALOG_DIR): {"bind": "/etc/trino/catalog", "mode": "ro"}},
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
        self.host_port = host_port

    @property
    def endpoint_url(self) -> str:
        """Trino HTTP endpoint (clients submit SQL via /v1/statement)."""
        return f"http://localhost:{self.host_port}"

    def query(self, sql: str, *, timeout: float = 60.0) -> dict:
        """Run a SQL query through Trino's REST API. Returns ``{columns, rows}`` or ``{error}``."""
        base = self.endpoint_url
        headers = {"X-Trino-User": "oblako", "Content-Type": "text/plain"}
        result = httpx.post(f"{base}/v1/statement", content=sql,
                            headers=headers, timeout=timeout).json()
        rows: list = []
        columns: list[str] | None = None
        deadline = time.time() + timeout
        while True:
            if "error" in result:
                return {"error": result["error"]}
            if columns is None and result.get("columns"):
                columns = [c["name"] for c in result["columns"]]
            rows.extend(result.get("data") or [])
            next_uri = result.get("nextUri")
            if not next_uri:
                break
            if time.time() > deadline:
                return {"error": {"message": "query timeout"}}
            result = httpx.get(next_uri, headers=headers, timeout=timeout).json()
        return {"columns": columns or [], "rows": rows}

    def _health_check(self) -> bool:
        try:
            resp = httpx.get(f"{self.endpoint_url}/v1/info", timeout=3.0)
            return resp.status_code == 200 and not resp.json().get("starting", True)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError):
            return False
