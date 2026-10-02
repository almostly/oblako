"""Local Trino — Athena-style SQL over the Iceberg REST catalog (= S3 Tables).

AWS Athena runs Trino/Presto-derived engines over S3. We expose the same
experience here: a Trino container with the Iceberg connector pre-wired to
oblako's Iceberg REST catalog and S3Proxy. ``SELECT * FROM iceberg.credit.applicants``
just works.

A second catalog, ``awsdatacatalog``, is the Hive connector over oblako's Glue
Data Catalog (Athena's ``AwsDataCatalog``): Parquet / CSV / JSON tables that
awswrangler or Athena CTAS register in Glue, with Iceberg tables redirected to
the ``iceberg`` catalog. The catalog configs are generated under
``~/.oblako/trino/catalog/`` and mounted into the container.
"""

from __future__ import annotations

from oblako import ports
import time
from pathlib import Path

import httpx

from .base import Service, PortMapping

TRINO_CATALOG_DIR = Path.home() / ".oblako" / "trino" / "catalog"

# Nested namespaces on: an S3 Tables table bucket is the first level of a two-level
# namespace [bucket, namespace], which Trino then shows as the schema "bucket.namespace".
_ICEBERG_PROPERTIES = """\
connector.name=iceberg
iceberg.catalog.type=rest
iceberg.rest-catalog.uri=http://host.docker.internal:8181
iceberg.rest-catalog.warehouse=s3://oblako-iceberg/
iceberg.rest-catalog.security=NONE
iceberg.rest-catalog.nested-namespace-enabled=true
fs.native-s3.enabled=true
s3.endpoint=http://host.docker.internal:9000
s3.region=us-east-1
s3.path-style-access=true
s3.aws-access-key=test
s3.aws-secret-key=test
"""

# Athena's AwsDataCatalog: Hive tables from oblako's Glue engine (on the host),
# Iceberg tables handed to the iceberg catalog above
_AWSDATACATALOG_PROPERTIES = f"""\
connector.name=hive
hive.metastore=glue
hive.metastore.glue.region=us-east-1
hive.metastore.glue.endpoint-url=http://host.docker.internal:{ports.GLUE_CATALOG}
hive.metastore.glue.aws-access-key=test
hive.metastore.glue.aws-secret-key=test
hive.iceberg-catalog-name=iceberg
hive.non-managed-table-writes-enabled=true
fs.native-s3.enabled=true
s3.endpoint=http://host.docker.internal:9000
s3.region=us-east-1
s3.path-style-access=true
s3.aws-access-key=test
s3.aws-secret-key=test
"""


class TrinoService(Service):
    """Local Trino, pre-wired with the Iceberg connector to oblako's catalog + S3."""

    def __init__(self, host_port: int = ports.TRINO):
        """Initialize on host_port (8485; Trino's internal port is 8080)."""
        TRINO_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
        (TRINO_CATALOG_DIR / "iceberg.properties").write_text(_ICEBERG_PROPERTIES)
        (TRINO_CATALOG_DIR / "awsdatacatalog.properties").write_text(
            _AWSDATACATALOG_PROPERTIES
        )
        super().__init__(
            name="trino",
            image="trinodb/trino:latest",
            ports=[PortMapping(container_port=8080, host_port=host_port)],
            volumes={
                str(TRINO_CATALOG_DIR): {"bind": "/etc/trino/catalog", "mode": "ro"}
            },
            environment={
                # S3Proxy doesn't implement aws-chunked CRC32 — make the AWS SDK v2
                # used by Trino's S3 client skip the new flexible checksums.
                "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
                "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
            },
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
        self.host_port = host_port

    def start(self) -> None:
        """Start the Glue engine (the awsdatacatalog metastore), then Trino."""
        from oblako.engines import host

        host.start("glue")
        super().start()

    @property
    def endpoint_url(self) -> str:
        """Trino HTTP endpoint (clients submit SQL via /v1/statement)."""
        return f"http://localhost:{self.host_port}"

    def query(
        self,
        sql: str,
        *,
        catalog: str | None = None,
        schema: str | None = None,
        session: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """Run SQL through Trino's REST API. Returns ``{columns, types, rows}`` or ``{error}``.

        ``catalog``/``schema`` set the Trino session defaults (so unqualified table
        names resolve), the way Athena's QueryExecutionContext does.
        """
        base = self.endpoint_url
        headers = {"X-Trino-User": "oblako", "Content-Type": "text/plain"}
        if catalog:
            headers["X-Trino-Catalog"] = catalog
        if schema:
            headers["X-Trino-Schema"] = schema
        if session:
            headers["X-Trino-Session"] = ",".join(
                f"{k}={v}" for k, v in session.items()
            )
        result = httpx.post(
            f"{base}/v1/statement", content=sql, headers=headers, timeout=timeout
        ).json()
        rows: list = []
        columns: list[str] | None = None
        types: list[str] = []
        deadline = time.time() + timeout
        while True:
            if "error" in result:
                return {"error": result["error"]}
            if columns is None and result.get("columns"):
                columns = [c["name"] for c in result["columns"]]
                types = [c["type"] for c in result["columns"]]
            rows.extend(result.get("data") or [])
            next_uri = result.get("nextUri")
            if not next_uri:
                break
            if time.time() > deadline:
                return {"error": {"message": "query timeout"}}
            result = httpx.get(next_uri, headers=headers, timeout=timeout).json()
        return {"columns": columns or [], "types": types, "rows": rows}

    def _health_check(self) -> bool:
        try:
            resp = httpx.get(f"{self.endpoint_url}/v1/info", timeout=3.0)
            return resp.status_code == 200 and not resp.json().get("starting", True)
        except (
            httpx.HTTPError
        ):  # any transport error (incl. accept-then-reset) = not ready
            return False
