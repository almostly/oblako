"""RDS / Aurora service: local Amazon RDS & Aurora.

Same pattern as Redshift — moto for the control plane + a real engine for the
data plane:
  * engine: a real PostgreSQL (default) or MySQL container (the actual database).
  * ``get_client()``: boto3 ``rds`` control plane (via moto) — create/describe
    **RDS instances** (`create_db_instance`) *and* **Aurora clusters**
    (`create_db_cluster`); returns endpoints, reader endpoints, members.
  * ``connect()``: a DB connection straight to the engine (psycopg2 / pymysql).
  * ``seed()``: re-create cluster/instance metadata (moto is in-memory) after a
    restart, idempotently.

The control-plane cluster/instance objects are moto metadata pointing at the one
local engine — real SQL behavior, simulated topology.
"""

from .base import Service, PortMapping

_ENGINES = {
    "postgres": {
        "image": "postgres:16",
        "container_port": 5432,
        "default_host_port": 5432,
        "data_dir": "/var/lib/postgresql/data",
        "volume": "oblako-ml-rds",
    },
    "mysql": {
        "image": "mysql:8.0",
        "container_port": 3306,
        "default_host_port": 3306,
        "data_dir": "/var/lib/mysql",
        "volume": "oblako-ml-rds-mysql",
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
        control_port: int = 5500,
        data_port: int = 8006,
        region: str = "us-east-1",
    ):
        """Initialize the RDS service for the specified engine (postgres or mysql)."""
        if engine not in _ENGINES:
            raise ValueError(f"engine must be one of {sorted(_ENGINES)}, got {engine!r}")
        spec = _ENGINES[engine]
        host_port = host_port or spec["default_host_port"]
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
            ports=[PortMapping(container_port=spec["container_port"], host_port=host_port)],
            environment=environment,
            volumes={spec["volume"]: {"bind": spec["data_dir"], "mode": "rw"}},
        )
        self.engine = engine
        self.host_port = host_port
        self.user = user
        self.password = password
        self.database = database
        self.control_port = control_port
        self.data_port = data_port
        self.region = region

    def connect(self):
        """Return a DB connection to the engine (psycopg2 for postgres, pymysql for mysql)."""
        if self.engine == "mysql":
            try:
                import pymysql
            except ImportError as e:
                raise ImportError(
                    "The MySQL engine needs pymysql: pip install 'oblako-ml[mysql]'"
                ) from e
            return pymysql.connect(
                host="localhost", port=self.host_port, user=self.user,
                password=self.password, database=self.database,
            )
        import psycopg2

        return psycopg2.connect(
            host="localhost", port=self.host_port, user=self.user,
            password=self.password, dbname=self.database,
        )

    def get_client(self):
        """boto3 ``rds`` control-plane client (RDS instances + Aurora clusters, via moto)."""
        import boto3

        return boto3.client(
            "rds",
            endpoint_url=f"http://localhost:{self.control_port}",
            region_name=self.region,
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )

    # -- declarative seed (moto metadata is in-memory; recreate after restart) --
    @staticmethod
    def _create_ignoring_exists(fn, **kwargs) -> bool:
        """Call fn(**kwargs); return True if created, False if it already existed."""
        try:
            fn(**kwargs)
            return True
        except Exception as e:  # noqa: BLE001
            if "AlreadyExists" in type(e).__name__:
                return False
            raise

    def seed(self, instances: list[dict] | None = None, clusters: list[dict] | None = None) -> dict:
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
                if self._create_ignoring_exists(rds.create_db_instance, DBClusterIdentifier=cid, **member):
                    created["instances"].append(member["DBInstanceIdentifier"])
        for spec in instances or []:
            if self._create_ignoring_exists(rds.create_db_instance, **spec):
                created["instances"].append(spec["DBInstanceIdentifier"])
        return created

    # -- RDS Data API (postgres or mysql engine) ---------------------------
    def start_data_server(self):
        """Start the rds-data server in-process (idempotent). Returns its URL."""
        from oblako_ml.rds_data import RdsDataExecutor, start_in_thread

        executor = RdsDataExecutor(
            host="localhost", port=self.host_port, user=self.user,
            password=self.password, database=self.database, engine=self.engine,
        )
        return start_in_thread(port=self.data_port, executor=executor)

    def get_data_client(self, autostart: bool = True):
        """boto3 ``rds-data`` client executing real SQL against the engine."""
        import boto3
        from oblako_ml import rds_data

        if autostart and not rds_data.is_running(self.data_port):
            self.start_data_server()
        return boto3.client(
            "rds-data",
            endpoint_url=f"http://localhost:{self.data_port}",
            region_name=self.region,
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )

    def _health_check(self) -> bool:
        try:
            conn = self.connect()
            conn.close()
            return True
        except Exception:  # noqa: BLE001 - driver-specific connection errors
            return False
