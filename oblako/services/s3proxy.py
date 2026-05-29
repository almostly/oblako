"""S3Proxy service: S3-compatible object storage."""

import httpx
from botocore.config import Config

from .base import Service, PortMapping
from . import boto


class S3ProxyService(Service):
    """S3-compatible object storage service backed by S3Proxy."""

    def __init__(self, host_port: int = 9000):
        """Initialize the S3Proxy service on the given host port."""
        super().__init__(
            name="s3proxy",
            image="andrewgaul/s3proxy:latest",
            ports=[PortMapping(container_port=80, host_port=host_port)],
            environment={
                "JCLOUDS_FILESYSTEM_BASEDIR": "/data",
                "S3PROXY_AUTHORIZATION": "none",
                # Browsers (dashboard / DuckDB-Wasm) need CORS to fetch parquet
                # from S3Proxy cross-origin; permissive is fine for local dev.
                "S3PROXY_CORS_ALLOW_ALL": "true",
            },
            volumes={"oblako-s3-data": {"bind": "/data", "mode": "rw"}},
        )
        self.host_port = host_port

    @property
    def endpoint_url(self) -> str:
        """Return the S3Proxy endpoint URL."""
        return f"http://localhost:{self.host_port}"

    def get_client(self):
        """Return a boto3 S3 client pointing at this S3Proxy.

        S3Proxy does not implement botocore's default flexible checksums
        (x-amz-checksum-crc32 over aws-chunked), so keep checksum calculation
        "when_required" — otherwise uploads fail with 501 NotImplemented.
        (Real checksum support would mean switching the backend to MinIO.)
        """
        return boto.client(
            "s3", self.endpoint_url,
            config=Config(
                signature_version="s3v4",
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def _health_check(self) -> bool:
        try:
            resp = httpx.get(self.endpoint_url, timeout=3.0)
            return resp.status_code in (200, 403)
        except httpx.HTTPError:
            # a starting service may accept then reset the connection
            # (httpx.ReadError), not just refuse it — any transport error = not ready
            return False
