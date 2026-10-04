"""Canonical host-port registry — the single source of truth for oblako's ports.

oblako exposes every service on a fixed, known host port: that's the
"unmodified ``boto3.client('s3')`` transparently hits ``localhost:9000``"
contract, and it's mirrored in ``docker-compose.yml`` and the notebook kernel's
``AWS_ENDPOINT_URL_*`` map. Defining each port once here (rather than as a literal
in every ``Service.__init__`` plus a duplicate list in ``notebook.py`` plus
hard-coded constants in the frontend) means the map can't drift.

These are deliberately *static* — a dynamic free-port allocator would break the
fixed-endpoint contract that boto3 clients, docker-compose, and the kernel all
depend on.
"""

# Object stores / streaming
S3 = 9000  # S3 endpoint: nginx, routing to S3Proxy and the S3 extensions engine
S3_BACKEND = 9001  # S3Proxy itself, behind the :9000 front
DYNAMODB = (
    8001  # DynamoDB Local (host; 8000 inside the container — 8000 is the dashboard)
)
KINESIS = 4567
OPENSEARCH = 9200

# Control plane — moto backs Redshift / RDS / Lambda / IAM / EC2 / API Gateway …
MOTO = 5500

# In-process API servers (started lazily by their Service)
CLOUDFORMATION = 8017  # not 5601, the OpenSearch Dashboards / Kibana port
REDSHIFT_DATA = 8002
RDS_DATA = 8006
BEDROCK_RUNTIME = 8004
GLUE_CATALOG = 8486
APPCONFIG = 8003  # appconfig (management) + appconfigdata (data)
SAGEMAKER = 8005  # sagemaker (control plane) + sagemaker-runtime + featurestore-runtime
DYNAMODB_VECTORS = 8007  # dynamodb vector-search proxy in front of DynamoDB Local
FIREHOSE = 8008  # kinesis Data Firehose (DirectPut -> S3), local delivery loop
ATHENA = 8009  # athena boto3 API executed via the Trino engine, results to S3
EVENTBRIDGE = 8010  # eventbridge proxy over moto that actually fires rule targets
ECS_METADATA = 8011  # ECS task metadata endpoint (ECS_CONTAINER_METADATA_URI) for tasks
S3_VECTORS = 8012  # S3 Vectors: vector buckets + indexes + k-NN QueryVectors
S3_TABLES = 8013  # S3 Tables: control plane over the Iceberg REST catalog
RDS_CONTROL = 8014  # RDS API over moto, a real PostgreSQL per DB instance
REDSHIFT_CONTROL = 8015  # Redshift API over moto, real multi-node clusters
MWAA = 8016  # MWAA API: AWS's Airflow containers per environment
ECS_CONTROL = 8018  # ECS API over moto that runs tasks as real containers
S3_EXT = 8020  # S3 tagging + Inventory (S3Proxy lacks them), reached through :9000

# Engines / data plane
OLLAMA = 11434  # Bedrock runtime is backed by Ollama
REDSHIFT_PG = 5439  # redshift image: wire proxy in front of PostgreSQL
RDS_PG = 5432
RDS_MYSQL = 3306
TRINO = 8485  # Athena equivalent
ICEBERG = 8181  # Iceberg REST catalog / S3 Tables

# Apps / orchestration
STEPFUNCTIONS = 8083
MLFLOW = 5050
CADDY = 80
DASHBOARD = 8000
NOTEBOOK = 8888


# Overrides: OBLAKO_PORT_<NAME>=<port> moves one service off its default port, such as
# RDS_PG when a local PostgreSQL already holds 5432. Every oblako process reads the same
# variable at import, so `oblako up`, `oblako configure` (which writes the profile's
# endpoints) and the engines agree; set it in your shell profile to keep it.
def _apply_overrides() -> None:
    import os

    names = {k for k, v in globals().items() if k.isupper() and isinstance(v, int)}
    for var, value in os.environ.items():
        if not var.startswith("OBLAKO_PORT_") or not value:
            continue  # an empty value means unset, as in the shell
        name = var.removeprefix("OBLAKO_PORT_")
        if name not in names:
            known = ", ".join(sorted(names))
            raise ValueError(f"{var}: no port named {name} (known: {known})")
        if not value.isdigit() or not 0 < int(value) < 65536:
            raise ValueError(f"{var}={value!r} is not a port number")
        globals()[name] = int(value)


def name_of(port: int) -> str | None:
    """Return the registry name of a host port, for messages (``5432`` -> ``RDS_PG``)."""
    return next((k for k, v in globals().items() if k.isupper() and v == port), None)


_apply_overrides()
