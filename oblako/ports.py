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
CLOUDFORMATION = 5601
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
