"""Redshift service: local Amazon Redshift on oblako's own image.

``oblako/redshift`` is a modern PostgreSQL 16 that impersonates Amazon Redshift:
a small ``shared_preload`` C extension registers the Redshift-only startup
parameters Amazon's ``redshift-connector`` driver sends (``client_protocol_version``
…) as no-op GUCs and reports ``server_version = 8.0.2``, so the driver connects
*natively* (no wire shim for the handshake; a bundled proxy on 5439 makes the
engine tolerate Redshift physical DDL). It also ships the Redshift system tables,
date/time functions, catalog views, UDFs (via plpython3u), and ``SET
query_group``. Built multi-arch, so it runs on Docker and Apple ``container``
alike. See ``oblako/images/redshift``.

Three ways in:
  * ``connect()``         - psycopg2 connection straight to the engine (5439).
  * ``get_client()``      - boto3 ``redshift`` control plane (clusters, nodes)
                            served by the local moto container.
  * ``get_data_client()`` - boto3 ``redshift-data`` client whose SQL executes
                            for real against the engine (auto-starts the server).
"""

from pathlib import Path

import psycopg2

from oblako import config, ports

from .base import PortMapping, Service

# Published image oblako pulls; falls back to building oblako/images/redshift
# locally if it isn't pullable yet (see Service.build_context).
REDSHIFT_IMAGE = "public.ecr.aws/oblako/redshift-local:16"
_BUILD_CONTEXT = str((Path(__file__).parent.parent / "images" / "redshift").resolve())

# The proxy's TLS cert inside the container. A fixed self-signed cert is baked
# into the image, so it's identical across containers, `down -v`, and clones.
SSL_CERT_PATH = "/etc/oblako-redshift/server.crt"


def _redshift_connector_bundle(python_exe: str) -> str:
    """Path to redshift_connector's trusted-CA bundle in the given interpreter."""
    import subprocess

    out = subprocess.check_output(
        [
            python_exe,
            "-c",
            "import os, redshift_connector as r; "
            "print(os.path.join(os.path.dirname(r.__file__), 'files', 'redshift-ca-bundle.crt'))",
        ],
        text=True,
    )
    return out.strip()


def append_cert_to_bundle(bundle_path: str, cert: str) -> bool:
    """Append a PEM cert to a CA bundle if not already present.

    redshift_connector has no CA-override option, but its bundle is a plain PEM
    file and ``load_verify_locations`` accepts extra certs appended to it. Returns
    True if the cert was added, False if it was already there (idempotent).
    """
    p = Path(bundle_path)
    text = p.read_text()
    cert = cert.strip()
    if cert in text:
        return False
    p.write_text(text.rstrip() + "\n" + cert + "\n")
    return True


class RedshiftService(Service):
    """Local Amazon Redshift, backed by the oblako/redshift image.

    A bundled wire proxy handles the Redshift-only SQL (physical DDL, varchar(max))
    and terminates TLS. libpq clients (psycopg2, JDBC) can use ``sslmode=require``;
    for redshift_connector (dbt, awswrangler) run ``oblako trust`` once per venv to
    trust the proxy's cert, then use ``sslmode=verify-ca``. See ``trust_cert``.
    """

    def __init__(
        self,
        host_port: int = ports.REDSHIFT_PG,
        user: str = "oblako",
        password: str = "oblako",
        database: str = "oblako",
        control_port: int = ports.MOTO,
        data_port: int = ports.REDSHIFT_DATA,
        region: str | None = None,
    ):
        """Initialize the Redshift engine container and connection settings."""
        super().__init__(
            name="redshift",
            image=REDSHIFT_IMAGE,
            build_context=_BUILD_CONTEXT,
            # Redshift's real port (5439) both on the host and inside the
            # container (the proxy listens there; PG is internal on 5433). Using
            # 5439, not 5432, avoids colliding with a plain Postgres co-located
            # in the same network (e.g. a Metabase metadata DB on Fargate).
            ports=[PortMapping(container_port=5439, host_port=host_port)],
            environment={
                "POSTGRES_USER": user,
                "POSTGRES_PASSWORD": password,
                "POSTGRES_DB": database,
                # Redshift uses md5 auth, which redshift-connector expects; the
                # image stores passwords as md5 to match (see its Dockerfile).
                "POSTGRES_HOST_AUTH_METHOD": "md5",
                # Where the in-engine COPY/UNLOAD bridge reaches the object store:
                # S3Proxy on the host (host.docker.internal is substituted for the
                # host gateway by the backend). Unset, it falls back to real AWS.
                "OBLAKO_S3_ENDPOINT": f"http://host.docker.internal:{ports.S3}",
            },
            volumes={
                "oblako-redshift-data": {
                    "bind": "/var/lib/postgresql/data",
                    "mode": "rw",
                },
                # Redshift ML: the in-container agent trains CREATE MODELs in a
                # container on the host Docker daemon (as moto does for Lambda).
                "/var/run/docker.sock": {
                    "bind": "/var/run/docker.sock",
                    "mode": "rw",
                },
            },
            # So the in-engine COPY/UNLOAD bridge can reach S3Proxy on the host
            # via host.docker.internal (Docker needs the explicit host-gateway
            # alias on Linux; the Apple backend rewrites it to the vmnet gateway).
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
        self.host_port = host_port
        self.user = user
        self.password = password
        self.database = database
        self.control_port = control_port
        self.data_port = data_port
        self.region = region or config.region()

    def connect(self):
        """Return a psycopg2 connection straight to the Redshift engine."""
        return psycopg2.connect(
            host="localhost",
            port=self.host_port,
            user=self.user,
            password=self.password,
            dbname=self.database,
        )

    def server_cert(self) -> str | None:
        """Read the proxy's self-signed TLS cert (PEM) from the container."""
        try:
            container = self.client.containers.get(self.container_name)
            code, out = container.exec_run(["cat", SSL_CERT_PATH])
        except Exception:  # noqa: BLE001 - not running / no docker
            return None
        return out.decode() if code == 0 else None

    def trust_cert(self, python_exe: str | None = None) -> str:
        """Trust the proxy's cert in a venv's redshift_connector CA bundle.

        redshift_connector (and thus dbt-redshift, awswrangler) verifies TLS
        against a hardcoded Amazon CA bundle with no override, so it can't verify
        a local cert out of the box. This appends the proxy's self-signed cert to
        that bundle in ``python_exe``'s environment (default: the current one), so
        ``sslmode=verify-ca`` then gives real, verified TLS locally, no
        ``ssl=False``. Idempotent. Re-run after a redshift-connector reinstall
        (which restores the pristine bundle). Note: that venv then also trusts this
        cert when talking to real Redshift (harmless without the proxy's key).
        """
        import sys

        cert = self.server_cert()
        if not cert:
            raise RuntimeError(
                f"couldn't read the TLS cert from {self.container_name}; "
                "is redshift running (oblako up redshift)?"
            )
        bundle = _redshift_connector_bundle(python_exe or sys.executable)
        added = append_cert_to_bundle(bundle, cert)
        return (
            f"appended oblako's Redshift cert to {bundle}"
            if added
            else f"already trusted in {bundle}"
        )

    def get_client(self):
        """boto3 ``redshift`` control-plane client (clusters/nodes via moto)."""
        from . import boto

        return boto.client(
            "redshift",
            f"http://localhost:{self.control_port}",
            region=self.region,
        )

    def start_data_server(self):
        """Start the redshift-data server in-process (idempotent). Returns its URL."""
        from oblako.engines.redshift_data import RedshiftDataExecutor, start_in_thread

        executor = RedshiftDataExecutor(
            host="localhost",
            port=self.host_port,
            user=self.user,
            password=self.password,
            database=self.database,
        )
        return start_in_thread(port=self.data_port, executor=executor)

    def get_data_client(self, autostart: bool = True):
        """boto3 ``redshift-data`` client executing real SQL against the engine."""
        from oblako.engines import redshift_data

        from . import boto

        if autostart and not redshift_data.is_running(self.data_port):
            self.start_data_server()
        return boto.client(
            "redshift-data",
            f"http://localhost:{self.data_port}",
            region=self.region,
        )

    def _health_check(self) -> bool:
        try:
            self.connect().close()
            return True
        except psycopg2.OperationalError:
            return False
