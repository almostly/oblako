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


class RedshiftService(Service):
    """Local Amazon Redshift, backed by the oblako/redshift image (no proxy)."""

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
            },
            volumes={
                "oblako-redshift-data": {
                    "bind": "/var/lib/postgresql/data",
                    "mode": "rw",
                }
            },
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
