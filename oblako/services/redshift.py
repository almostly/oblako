"""Redshift service: local Amazon Redshift replacement via pgredshift.

Two containers make up the local Redshift:

  * ``PgRedshiftService`` — the raw engine, ``hearthsim/pgredshift`` (a
    PostgreSQL 10 image that adds Redshift system tables (STL/STV),
    ``SET query_group``, and Redshift UDFs). Published on port 5438 for direct
    psql/debug access.
  * ``RedshiftService`` — the Redshift *endpoint* on port 5439 (Redshift's
    port). It's a thin Postgres-wire proxy (``oblako.engines.redshift_proxy``)
    in front of the engine: transparent for psycopg2, but it fixes up the
    handshake so the ``redshift-connector`` driver (used by dbt-redshift and
    other Redshift-from-Python tools) — which pgredshift would otherwise
    reject — connects too. Everyone connects here.

Three ways in (all via the 5439 endpoint):
  * ``connect()``         - psycopg2 connection to Redshift.
  * ``get_client()``      - boto3 ``redshift`` control plane (clusters, nodes)
                            served by the local moto container.
  * ``get_data_client()`` - boto3 ``redshift-data`` client whose SQL executes
                            for real against pgredshift (auto-starts the server).
"""

from pathlib import Path

import psycopg2

from oblako import config, ports
from oblako.engines import redshift_proxy

from .base import PortMapping, Service


class PgRedshiftService(Service):
    """Raw pgredshift engine, behind the Redshift proxy (direct/debug access)."""

    def __init__(
        self,
        host_port: int = ports.REDSHIFT_ENGINE,
        user: str = "oblako",
        password: str = "oblako",
        database: str = "oblako",
    ):
        """Initialize the pgredshift engine container on the given host port."""
        super().__init__(
            name="pgredshift",
            image="hearthsim/pgredshift:latest",
            # pgredshift listens on the standard 5432 inside the container;
            # expose it on 5438 — the Redshift proxy (5439) sits in front.
            ports=[PortMapping(container_port=5432, host_port=host_port)],
            environment={
                "POSTGRES_USER": user,
                "POSTGRES_PASSWORD": password,
                "POSTGRES_DB": database,
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

    def connect(self):
        """Return a psycopg2 connection straight to the raw engine."""
        return psycopg2.connect(
            host="localhost",
            port=self.host_port,
            user=self.user,
            password=self.password,
            dbname=self.database,
        )

    def _health_check(self) -> bool:
        try:
            self.connect().close()
            return True
        except psycopg2.OperationalError:
            return False


class RedshiftService(Service):
    """Local Amazon Redshift endpoint: the wire proxy in front of pgredshift."""

    def __init__(
        self,
        host_port: int = ports.REDSHIFT_PG,
        engine_port: int = ports.REDSHIFT_ENGINE,
        user: str = "oblako",
        password: str = "oblako",
        database: str = "oblako",
        control_port: int = ports.MOTO,
        data_port: int = ports.REDSHIFT_DATA,
        region: str | None = None,
    ):
        """Initialize the Redshift endpoint (proxy) and connection settings."""
        # The proxy is a plain-stdlib script; run it in a minimal python image
        # with the module bind-mounted, reaching the engine over the host.
        proxy_script = str(Path(redshift_proxy.__file__).resolve())
        super().__init__(
            name="redshift",
            image="python:3.12-slim",
            ports=[PortMapping(container_port=host_port, host_port=host_port)],
            environment={
                "OBLAKO_RS_PROXY_LISTEN": str(host_port),
                "OBLAKO_RS_PROXY_BACKEND_HOST": "host.docker.internal",
                "OBLAKO_RS_PROXY_BACKEND_PORT": str(engine_port),
            },
            volumes={proxy_script: {"bind": "/proxy.py", "mode": "ro"}},
            extra_hosts={"host.docker.internal": "host-gateway"},
            command=["python", "/proxy.py"],
        )
        self.host_port = host_port
        self.engine_port = engine_port
        self.user = user
        self.password = password
        self.database = database
        self.control_port = control_port
        self.data_port = data_port
        self.region = region or config.region()

    def connect(self):
        """Return a psycopg2 connection to the Redshift endpoint."""
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
        """boto3 ``redshift-data`` client executing real SQL against pgredshift."""
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
        # Connecting through the endpoint exercises both the proxy and the engine.
        try:
            self.connect().close()
            return True
        except psycopg2.OperationalError:
            return False
