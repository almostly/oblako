"""S3 service: S3Proxy, with tagging and Inventory added in front of it.

S3Proxy stores the objects but doesn't implement object / bucket tagging or S3
Inventory. On a Docker-API backend, ``oblako up s3`` therefore runs three parts
behind the one S3 endpoint (:9000):

- a stock nginx on :9000 that passes every request straight to S3Proxy,
  except tagging / Inventory / bucket policy / notification requests, and
  logs completed writes for event notifications (see ``_nginx_conf``). nginx rather than
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
    """Route S3Proxy's gaps to the extensions engine, everything else to S3Proxy.

    ``?tagging`` / ``?inventory`` / ``?policy`` / ``?notification`` match as a
    query parameter name (any value, incl. none), so a listing with
    ``prefix=tagging`` still goes to S3Proxy. Bodies stream in both directions
    (no buffering, no size limit). Completed writes are logged as JSON lines,
    which the engine follows to fire event notifications.
    """
    # @HOST@ is filled in at container start with host.docker.internal's IPv4
    # address (see _FRONT_COMMAND): Docker Desktop also maps it to an IPv6 one
    # the host isn't reachable on, and nginx would alternate between the two
    up = "@HOST@"
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
    access_log off;
    # completed writes, one JSON line each, for event notifications
    map $request_method $s3_write {{
        PUT 1;
        POST 1;
        DELETE 1;
        default 0;
    }}
    log_format s3_writes escape=json '{{"method":"$request_method",'
        '"uri":"$s3_path","status":"$status","etag":"$upstream_http_etag",'
        '"length":"$content_length","copy":"$http_x_amz_copy_source"}}';
    upstream s3proxy {{ server {up}:{backend_port}; keepalive 32; }}
    upstream s3ext {{ server {up}:{ext_port}; keepalive 8; }}
    map $args $ext_query {{
        "~(^|&)(tagging|inventory|policy|policyStatus|notification)(=|&|$)" 1;
        default 0;
    }}
    # S3 Control (its tag API, at /v20180820/) is answered by the extensions engine
    map $uri $s3_control {{
        "~^/v20180820/" 1;
        default 0;
    }}
    # CreateBucket (PUT on a bucket, no key, no query): S3 in us-east-1 answers
    # 200 for a bucket you already own, S3Proxy 409 BucketAlreadyOwnedByYou
    map "$request_method:$s3_path" $create_bucket {{
        "~^PUT:/[^/?]+/?$" 1;
        default 0;
    }}
    map "$s3_control:$ext_query:$create_bucket:$http_x_amz_tagging:$http_x_amz_copy_source" $s3_upstream {{
        "0:0:0::" s3proxy;
        default s3ext;
    }}
    # virtual-hosted addressing (bucket.localhost:9000/key), the AWS SDKs'
    # default outside Python: the bucket moves into the path, so everything
    # behind the front sees path-style requests
    map "$s3_control:$http_host" $vhost_bucket {{
        "~^0:(?<bucket>[a-z0-9][a-z0-9.-]*[a-z0-9])\\.localhost(:[0-9]+)?$" $bucket;
        default "";
    }}
    map $vhost_bucket $s3_path {{
        "" $request_uri;
        default /$vhost_bucket$request_uri;
    }}
    server {{
        listen 80;
        location / {{
            access_log /var/log/oblako/writes.log s3_writes if=$s3_write;
            proxy_set_header Host $http_host;
            proxy_set_header Connection "";
            # S3Proxy rejects this (NotImplemented) and runs without auth anyway;
            # boto3 sends it with temporary credentials: Lambda, SSO, roles
            proxy_set_header X-Amz-Security-Token "";
            # newer AWS SDKs ask ListObjectsV2 for RestoreStatus with this;
            # S3Proxy 501s it (Trino, Spark/Glue S3A); unset, S3 omits the field
            proxy_set_header X-Amz-Optional-Object-Attributes "";
            # $request_uri keeps the client's percent-encoding of the key
            proxy_pass http://$s3_upstream$s3_path;
        }}
    }}
}}
"""


_FRONT_COMMAND = [
    "sh",
    "-c",
    "HOST=$(getent ahostsv4 host.docker.internal | awk 'NR==1{print $1}'); "
    'sed "s/@HOST@/${HOST:-host.docker.internal}/g" '
    "/etc/nginx/oblako.conf.template > /etc/nginx/nginx.conf "
    "&& exec nginx -g 'daemon off;'",
]


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
                # file names are object keys: without a UTF-8 locale the JVM
                # cannot store a key such as "café.txt", which S3 accepts
                "LANG": "C.UTF-8",
                "JAVA_TOOL_OPTIONS": "-Dsun.jnu.encoding=UTF-8 -Dfile.encoding=UTF-8",
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
                    "bind": "/etc/nginx/oblako.conf.template",
                    "mode": "ro",
                },
                # completed writes, followed by the engine for notifications
                str(FRONT_DIR / "log"): {"bind": "/var/log/oblako", "mode": "rw"},
            },
            extra_hosts={"host.docker.internal": "host-gateway"},
            command=_FRONT_COMMAND,
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
        (FRONT_DIR / "log").mkdir(parents=True, exist_ok=True)
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
