"""S3 service: S3Proxy, with tagging and Inventory added in front of it.

S3Proxy stores the objects but doesn't implement object / bucket tagging or S3
Inventory. On a Docker-API backend, ``oblako up s3`` therefore runs three parts
behind the one S3 endpoint (:9000):

- a stock nginx on :9000 that passes every request straight to S3Proxy,
  except tagging / Inventory requests (see ``_nginx_conf``). nginx rather than
  Caddy because Go's HTTP stack rewrites header names to Title-Case, which
  boto3 then surfaces as metadata keys (``Source-Job`` instead of ``source-job``);
- S3Proxy itself, on :9001 (``ports.S3_BACKEND``);
- the S3 extensions engine (``oblako.engines.s3_ext``), which answers those
  requests and runs as a background process like ``oblako up s3vectors``.

Set ``OBLAKO_S3_EXTENSIONS=0`` to serve S3Proxy directly on :9000 instead (no
tagging or Inventory). Other backends (Kubernetes, Apple ``container``) always
do, since their containers can't reach a host process the same way.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from botocore.config import Config

from oblako import ports

from . import boto
from .backends import ABSENT, DockerBackend, get_backend
from .base import PortMapping, Service, ServiceStatus

FRONT_DIR = Path.home() / ".oblako" / "s3-front"


def _nginx_conf(backend_port: int, ext_port: int) -> str:
    """Route tagging / Inventory to the extensions engine, the rest to S3Proxy.

    ``?tagging`` / ``?inventory`` match as a query parameter name (any value,
    incl. none), so a listing with ``prefix=tagging`` still goes to S3Proxy.
    Bodies stream in both directions (no buffering, no size limit).
    """
    up = "host.docker.internal"
    return f"""worker_processes auto;
events {{ worker_connections 1024; }}
http {{
    client_max_body_size 0;
    proxy_request_buffering off;
    proxy_buffering off;
    proxy_http_version 1.1;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    ignore_invalid_headers off;
    upstream s3proxy {{ server {up}:{backend_port}; keepalive 32; }}
    upstream s3ext {{ server {up}:{ext_port}; keepalive 8; }}
    map $args $ext_query {{
        "~(^|&)(tagging|inventory)(=|&|$)" 1;
        default 0;
    }}
    map "$ext_query:$http_x_amz_tagging:$http_x_amz_copy_source" $s3_upstream {{
        "0::" s3proxy;
        default s3ext;
    }}
    server {{
        listen 80;
        location / {{
            proxy_set_header Host $http_host;
            proxy_set_header Connection "";
            proxy_pass http://$s3_upstream;
        }}
    }}
}}
"""


class S3ProxyService(Service):
    """S3-compatible object storage: S3Proxy, plus tagging and Inventory."""

    def __init__(
        self,
        host_port: int = ports.S3,
        backend_port: int = ports.S3_BACKEND,
        extensions: bool | None = None,
    ):
        """Initialize the S3 service; the endpoint is ``host_port``."""
        backend = get_backend()
        if extensions is None:
            extensions = os.environ.get("OBLAKO_S3_EXTENSIONS", "1") != "0"
        self.extensions = extensions and isinstance(backend, DockerBackend)
        super().__init__(
            name="s3proxy",
            # Pinned: an existing :latest is never re-pulled, so it goes stale.
            # 4.1.x stops URL-encoding the ListObjectsV2 continuation token,
            # which made paginating >1,000 keys containing "=" loop forever.
            image="andrewgaul/s3proxy:s3proxy-4.1.1",
            ports=[
                PortMapping(
                    container_port=80,
                    host_port=backend_port if self.extensions else host_port,
                )
            ],
            environment={
                "JCLOUDS_FILESYSTEM_BASEDIR": "/data",
                "S3PROXY_AUTHORIZATION": "none",
                # Browsers (dashboard / DuckDB-Wasm) need CORS to fetch parquet
                # from S3Proxy cross-origin; permissive is fine for local dev.
                "S3PROXY_CORS_ALLOW_ALL": "true",
            },
            volumes={"oblako-s3-data": {"bind": "/data", "mode": "rw"}},
            backend=backend,
        )
        self.host_port = host_port
        self.backend_port = backend_port
        self.front = Service(
            name="s3",
            image="nginx:alpine",
            ports=[PortMapping(container_port=80, host_port=host_port)],
            volumes={
                str(FRONT_DIR / "nginx.conf"): {
                    "bind": "/etc/nginx/nginx.conf",
                    "mode": "ro",
                }
            },
            extra_hosts={"host.docker.internal": "host-gateway"},
            backend=backend,
        )

    @property
    def endpoint_url(self) -> str:
        """Return the S3 endpoint URL."""
        return f"http://localhost:{self.host_port}"

    def _published_port(self) -> int | None:
        """Return the host port S3Proxy's container publishes, if it's running."""
        try:
            bound = self.client.containers.get(self.container_name).ports.get("80/tcp")
            return int(bound[0]["HostPort"]) if bound else None
        except Exception:
            return None

    def start(self) -> None:
        """Start S3Proxy, and with extensions the engine and the :9000 front."""
        expected = self.ports[0].host_port
        published = self._published_port()
        if published is not None and published != expected:
            # an S3Proxy from before the front (published on :9000): recreate
            # it on its own port; the data volume is kept
            super().stop()
        super().start()
        if not self.extensions:
            return
        from oblako.engines import host

        host.start("s3-ext")
        FRONT_DIR.mkdir(parents=True, exist_ok=True)
        (FRONT_DIR / "nginx.conf").write_text(
            _nginx_conf(self.backend_port, ports.S3_EXT)
        )
        self.front.start()

    def stop(self) -> None:
        """Stop the front, the extensions engine and S3Proxy."""
        if self.extensions:
            from oblako.engines import host

            if self.front.backend.status(self.front.container_name) != ABSENT:
                self.front.stop()
            host.stop("s3-ext")
        super().stop()

    def status(self) -> ServiceStatus:
        """Return RUNNING only when every part is up."""
        state = super().status()
        if not self.extensions or state != ServiceStatus.RUNNING:
            return state
        return self.front.status()

    def get_client(self):
        """Return a boto3 S3 client pointing at the S3 endpoint.

        S3Proxy does not implement botocore's default flexible checksums
        (x-amz-checksum-crc32 over aws-chunked), so keep checksum calculation
        "when_required"; otherwise uploads fail with 501 NotImplemented.
        """
        return boto.client(
            "s3",
            self.endpoint_url,
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
            # (httpx.ReadError), not just refuse it; any transport error = not ready
            return False
