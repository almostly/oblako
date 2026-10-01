"""Local Iceberg REST catalog — Iceberg tables on S3Proxy.

On AWS this is **S3 Tables** (``boto3.client("s3tables")``): a managed Iceberg
catalog over S3. Same shape locally — Iceberg REST + S3Proxy underneath; the
platform aliases this service as ``oblako.s3tables`` to mirror the AWS API.

The ``tabulario/iceberg-rest`` reference image speaks the Iceberg REST API on
:8181, with a built-in JDBC catalog. Configured here so the *warehouse* is
``s3://oblako-iceberg/`` on S3Proxy, with path-style addressing and the same
creds the rest of oblako uses.

pyiceberg clients connect via ``FsspecFileIO`` (s3fs/boto3 — honours
``AWS_REQUEST_CHECKSUM_CALCULATION=when_required`` which S3Proxy needs;
pyarrow's S3FS doesn't and the multipart upload fails with HTTP 400):

    import os
    os.environ.update(AWS_REQUEST_CHECKSUM_CALCULATION="when_required",
                      AWS_RESPONSE_CHECKSUM_VALIDATION="when_required")
    from pyiceberg.catalog import load_catalog
    cat = load_catalog("oblako",
                       uri="http://localhost:8181",
                       warehouse="s3://oblako-iceberg/",
                       **{"py-io-impl": "pyiceberg.io.fsspec.FsspecFileIO",
                          "s3.endpoint": "http://localhost:9000",
                          "s3.access-key-id": "test",
                          "s3.secret-access-key": "test",
                          "s3.path-style-access": "true"})
"""

from __future__ import annotations

from oblako import ports
import httpx

from oblako import config
from .base import Service, PortMapping

DEFAULT_WAREHOUSE = "s3://oblako-iceberg/"


class IcebergCatalogService(Service):
    """Local Iceberg REST catalog backed by S3Proxy."""

    def __init__(
        self,
        host_port: int = ports.ICEBERG,
        s3_endpoint: str = "http://host.docker.internal:9000",
        warehouse: str = DEFAULT_WAREHOUSE,
    ):
        """Initialize on host_port (8181) with the warehouse on S3Proxy."""
        super().__init__(
            name="iceberg",
            image="tabulario/iceberg-rest:latest",
            ports=[PortMapping(container_port=8181, host_port=host_port)],
            environment={
                "AWS_ACCESS_KEY_ID": "test",
                "AWS_SECRET_ACCESS_KEY": "test",
                "AWS_REGION": config.region(),
                "CATALOG_WAREHOUSE": warehouse,
                "CATALOG_IO__IMPL": "org.apache.iceberg.aws.s3.S3FileIO",
                "CATALOG_S3_ENDPOINT": s3_endpoint,
                "CATALOG_S3_PATH-STYLE-ACCESS": "true",
            },
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
        self.host_port = host_port
        self.warehouse = warehouse

    def start(self) -> None:
        """Start the catalog and create its warehouse bucket if it's missing."""
        super().start()
        self.ensure_warehouse()

    def ensure_warehouse(self) -> None:
        """Create the warehouse bucket on S3, where every table's files go.

        The REST catalog places a namespace's tables under the warehouse, and
        writes there fail with NoSuchBucket until the bucket exists. Needs S3
        running; without it this only says so.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        from .s3proxy import S3ProxyService

        bucket = self.warehouse.removeprefix("s3://").split("/", 1)[0]
        s3 = S3ProxyService().get_client()
        try:
            s3.head_bucket(Bucket=bucket)
        except ClientError:
            s3.create_bucket(Bucket=bucket)
        except BotoCoreError as err:
            print(
                f"iceberg: S3 isn't reachable, create s3://{bucket} once it is ({err})"
            )

    @property
    def endpoint_url(self) -> str:
        """REST catalog endpoint that pyiceberg / Spark Iceberg connect to."""
        return f"http://localhost:{self.host_port}"

    def _health_check(self) -> bool:
        try:
            resp = httpx.get(f"{self.endpoint_url}/v1/config", timeout=3.0)
            return resp.status_code == 200
        except (
            httpx.HTTPError
        ):  # any transport error (incl. accept-then-reset) = not ready
            return False
