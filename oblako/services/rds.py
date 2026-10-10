"""RDS / Aurora service: local Amazon RDS & Aurora.

Same pattern as Redshift: an RDS control plane plus real engines for the data
plane.
  * engine: a real PostgreSQL (default) or MySQL container, the shared database
    behind Aurora clusters and instances that oblako doesn't run on their own.
  * ``get_client()``: boto3 ``rds`` through the rds-control proxy over moto.
    Create/describe **RDS instances** and **Aurora clusters**; each standalone
    PostgreSQL instance runs in its own container, with streaming read replicas
    and parameter-group ``wal_level`` (see ``oblako.engines.rds_control``).
  * ``connect()``: a DB connection straight to the engine (psycopg2 / pymysql).
  * ``seed()``: re-create cluster/instance metadata (moto is in-memory) after a
    restart, idempotently.

Aurora cluster objects are moto metadata pointing at the one local engine: real
SQL behavior, simulated topology.
"""

import importlib
from typing import TypedDict

from oblako import config, ports

from .base import PortMapping, Service

# PostgreSQL 16 with pgvector, as RDS and Aurora PostgreSQL offer it
# (CREATE EXTENSION vector): pgvector 0.8.7 on postgres:16, pinned by digest
# (multi-arch). The trixie variant on purpose: postgres:16 is built on Debian
# trixie, and data volumes it created would hit a collation (glibc) version
# mismatch on the bookworm variant.
POSTGRES_IMAGE = (
    "pgvector/pgvector"
    "@sha256:40d828eda66737aaacb934387a337babf1455ebd56e9bdd13fc08e211257c972"
)


class _EngineSpec(TypedDict):
    """Container settings for one RDS database engine."""

    image: str
    container_port: int
    default_host_port: int
    data_dir: str
    volume: str


_ENGINES: dict[str, _EngineSpec] = {
    "postgres": {
        "image": POSTGRES_IMAGE,
        "container_port": 5432,
        "default_host_port": ports.RDS_PG,
        "data_dir": "/var/lib/postgresql/data",
        "volume": "oblako-rds-data",
    },
    "mysql": {
        "image": "mysql:8.0",
        "container_port": 3306,
        "default_host_port": ports.RDS_MYSQL,
        "data_dir": "/var/lib/mysql",
        "volume": "oblako-rds-mysql-data",
    },
}


class RdsService(Service):
    """Local Amazon RDS and Aurora service backed by a real PostgreSQL or MySQL engine."""

    def __init__(
        self,
        engine: str = "postgres",
        host_port: int | None = None,
        user: str = "oblako",
        password: str = "oblako",
        database: str = "oblako",
        control_port: int = ports.RDS_CONTROL,
        data_port: int = ports.RDS_DATA,
        region: str | None = None,
    ):
        """Initialize the RDS service for the specified engine (postgres or mysql)."""
        if engine not in _ENGINES:
            raise ValueError(
                f"engine must be one of {sorted(_ENGINES)}, got {engine!r}"
            )
        spec = _ENGINES[engine]
        port: int = host_port or spec["default_host_port"]
        if engine == "postgres":
            environment = {
                "POSTGRES_USER": user,
                "POSTGRES_PASSWORD": password,
                "POSTGRES_DB": database,
            }
        else:  # mysql
            environment = {
                "MYSQL_ROOT_PASSWORD": password,
                "MYSQL_DATABASE": database,
                "MYSQL_USER": user,
                "MYSQL_PASSWORD": password,
            }
        super().__init__(
            name="rds",
            image=spec["image"],
            ports=[PortMapping(container_port=spec["container_port"], host_port=port)],
            environment=environment,
            volumes={spec["volume"]: {"bind": spec["data_dir"], "mode": "rw"}},
        )
        self.engine = engine
        self.host_port = port
        self.user = user
        self.password = password
        self.database = database
        self.control_port = control_port
        self.data_port = data_port
        self.region = region or config.region()

    def connect(self):
        """Return a DB connection to the engine (psycopg2 for postgres, pymysql for mysql)."""
        if self.engine == "mysql":
            try:
                # optional extra (oblako[mysql]), imported only when asked for
                pymysql = importlib.import_module("pymysql")
            except ImportError as e:
                raise ImportError(
                    "The MySQL engine needs pymysql: pip install 'oblako[mysql]'"
                ) from e
            return pymysql.connect(
                host="localhost",
                port=self.host_port,
                user=self.user,
                password=self.password,
                database=self.database,
            )
        import psycopg2

        return psycopg2.connect(
            host="localhost",
            port=self.host_port,
            user=self.user,
            password=self.password,
            dbname=self.database,
        )

    def start(self) -> None:
        """Start the engine, then the RDS control plane that runs one per DB instance."""
        from oblako.engines import host

        super().start()
        if self.control_port == ports.RDS_CONTROL:
            host.start("rds-control")

    def get_client(self):
        """boto3 ``rds`` control-plane client (RDS instances + Aurora clusters)."""
        from . import boto

        if self.control_port == ports.RDS_CONTROL:
            from oblako.engines import rds_control

            rds_control.start_in_thread(self.control_port)
        return boto.client(
            "rds",
            f"http://localhost:{self.control_port}",
            region=self.region,
        )

    # -------------------------------------------------------------------------------
    # Declarative seed (moto metadata is in-memory; recreate after restart)
    # -------------------------------------------------------------------------------
    @staticmethod
    def _create_ignoring_exists(fn, **kwargs) -> bool:
        """Call fn(**kwargs); return True if created, False if it already existed."""
        try:
            fn(**kwargs)
            return True
        except Exception as e:
            if "AlreadyExists" in type(e).__name__:
                return False
            raise

    def seed(
        self, instances: list[dict] | None = None, clusters: list[dict] | None = None
    ) -> dict:
        """Idempotently (re)create RDS instances and Aurora clusters from a spec.

        ``clusters`` entries may carry an ``"instances"`` list of member specs.
        Each dict is passed through as kwargs to ``create_db_cluster`` /
        ``create_db_instance``. Safe to re-run after a moto restart.
        """
        rds = self.get_client()
        created: dict[str, list[str]] = {"clusters": [], "instances": []}
        for spec in clusters or []:
            spec = dict(spec)
            members = spec.pop("instances", [])
            cid = spec["DBClusterIdentifier"]
            if self._create_ignoring_exists(rds.create_db_cluster, **spec):
                created["clusters"].append(cid)
            for member in members:
                if self._create_ignoring_exists(
                    rds.create_db_instance, DBClusterIdentifier=cid, **member
                ):
                    created["instances"].append(member["DBInstanceIdentifier"])
        for spec in instances or []:
            if self._create_ignoring_exists(rds.create_db_instance, **spec):
                created["instances"].append(spec["DBInstanceIdentifier"])
        return created

    # -------------------------------------------------------------------------------
    # RDS Data API (postgres or mysql engine)
    # -------------------------------------------------------------------------------
    def start_data_server(self):
        """Start the rds-data server in-process (idempotent). Returns its URL."""
        from oblako.engines.rds_data import RdsDataExecutor, start_in_thread

        executor = RdsDataExecutor(
            host="localhost",
            port=self.host_port,
            user=self.user,
            password=self.password,
            database=self.database,
            engine=self.engine,
        )
        return start_in_thread(port=self.data_port, executor=executor)

    def get_data_client(self, autostart: bool = True):
        """boto3 ``rds-data`` client executing real SQL against the engine."""
        from oblako.engines import rds_data

        from . import boto

        if autostart and not rds_data.is_running(self.data_port):
            self.start_data_server()
        return boto.client(
            "rds-data",
            f"http://localhost:{self.data_port}",
            region=self.region,
        )

    def _health_check(self) -> bool:
        """Return True if the database accepts a connection."""
        try:
            conn = self.connect()
            conn.close()
            return True
        except Exception:  # driver-specific connection errors
            return False
