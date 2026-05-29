"""Redshift service: local Amazon Redshift replacement via pgredshift.

Uses hearthsim/pgredshift, a PostgreSQL 10 image that adds Redshift system
tables (STL/STV), `SET query_group`, and a handful of Redshift UDFs. It speaks
the PostgreSQL wire protocol, so psycopg2 connects to it like any RDS instance,
but on Redshift's port (5439).

Three ways in:
  * ``connect()``         - psycopg2 connection straight to pgredshift.
  * ``get_client()``      - boto3 ``redshift`` control plane (clusters, nodes)
                            served by the local moto container.
  * ``get_data_client()`` - boto3 ``redshift-data`` client whose SQL executes
                            for real against pgredshift (auto-starts the server).
"""

import psycopg2

from oblako import config
from .base import Service, PortMapping


class RedshiftService(Service):
    """Local Amazon Redshift replacement backed by pgredshift."""

    def __init__(
        self,
        host_port: int = 5439,
        user: str = "oblako",
        password: str = "oblako",
        database: str = "oblako",
        control_port: int = 5500,
        data_port: int = 8002,
        region: str | None = None,
    ):
        """Initialize the Redshift service with connection and port settings."""
        super().__init__(
            name="redshift",
            image="hearthsim/pgredshift:latest",
            # pgredshift listens on the standard 5432 inside the container;
            # expose it on Redshift's 5439 on the host.
            ports=[PortMapping(container_port=5432, host_port=host_port)],
            environment={
                "POSTGRES_USER": user,
                "POSTGRES_PASSWORD": password,
                "POSTGRES_DB": database,
            },
            volumes={
                "oblako-redshift-data": {"bind": "/var/lib/postgresql/data", "mode": "rw"}
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
        """Return a psycopg2 connection to this instance."""
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
            "redshift", f"http://localhost:{self.control_port}", region=self.region,
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
            "redshift-data", f"http://localhost:{self.data_port}", region=self.region,
        )

    def _health_check(self) -> bool:
        try:
            conn = self.connect()
            conn.close()
            return True
        except psycopg2.OperationalError:
            return False
