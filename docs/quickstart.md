# Quick start

## Prerequisites

- **Docker**: oblako runs services as local containers.
- **Python 3.10+**

## Install

```bash
pip install oblako
```

Or from source:

```bash
git clone https://github.com/almostly/oblako && cd oblako
pip install -e .
```

## Start the services

```bash
oblako up                  # start all services
oblako pull qwen2.5:0.5b   # pull a model into the Bedrock (Ollama) engine
oblako dashboard           # web UI at http://localhost:8000
```

## Your AWS code just works

Each service is reached through its normal `boto3` client. In a notebook or with
the endpoint env vars set, unmodified `boto3` transparently hits the local
service, no `endpoint_url`:

```python
import boto3

s3 = boto3.client("s3")          # -> S3Proxy
s3.create_bucket(Bucket="demo")

ddb = boto3.client("dynamodb")   # -> DynamoDB Local
ddb.list_tables()
```

Or go through the service handles, which also expose native drivers:

```python
from oblako.services import Oblako

o = Oblako()
o.redshift.get_client()    # boto3.client("redshift") -> moto control plane
o.redshift.connect()       # psycopg2 / redshift-connector -> the real engine
```

## Start a single service

```bash
oblako up redshift          # just Redshift
oblako logs opensearch      # tail a service's logs
oblako down stepfunctions   # stop one service
```

Service names: `bedrock`, `opensearch`, `redshift`, `rds`, `moto`, `s3`,
`dynamodb`, `kinesis`, `stepfunctions`, `iceberg` (the Iceberg REST catalog) and
`trino` (Athena's engine; it also starts the Glue engine). `ollama` aliases
`bedrock`; `aurora` aliases `rds` (it also starts the RDS API on :8014, which
runs a PostgreSQL container per DB instance). A bare `oblako up` starts all of
these except `iceberg` and `trino`.

The API engines that oblako runs in Python rather than in a container start the
same way, each on its canonical port, so plain boto3 or the AWS CLI can reach
them without any oblako code in the client: `s3vectors`, `s3tables`, `athena`,
`firehose`, `eventbridge`, `appconfig`, `sagemaker`, `glue`,
`dynamodb-vectors`, `redshift-data`, `rds-data`, `rds-control`,
`bedrock-runtime`, `cloudformation`, `ecs-metadata`.

```bash
oblako up s3vectors         # S3 Vectors on :8012, in the background
oblako logs s3vectors       # its log (~/.oblako/logs/s3vectors.log)
oblako down s3vectors
```

(Code that uses oblako's Python API starts these engines on demand.)

## CLI reference

| Command | Description |
|---|---|
| `oblako up [service]` | Start all services, or one (a container service like `s3`, or an API engine like `s3vectors`) |
| `oblako down [service]` | Stop all services (or a specific one) |
| `oblako status` | Show service status |
| `oblako dashboard [-p PORT]` | Start the web dashboard (default: 8000) |
| `oblako notebook [-p PORT]` | Launch JupyterLab wired to oblako (default: 8888) |
| `oblako redshift-data [-p PORT]` | Start the Redshift Data API server (default: 8002) |
| `oblako bedrock-runtime [-p PORT]` | Start the Bedrock Runtime server (default: 8004) |
| `oblako rds-data [-p PORT]` | Start the RDS Data API server (default: 8006) |
| `oblako cloudformation [-p PORT]` | Start the CloudFormation server (default: 5601) |
| `oblako agentcore run <file>` | Run a local AgentCore agent (default: 8080) |
| `oblako logs <service>` | Show logs for a service |
| `oblako pull [model]` | Pull a model into the engine (default: `qwen2.5:0.5b`) |
| `oblako models` | List available Ollama models |
| `oblako test` | Run unit tests |
| `oblako test-integration` | Run integration tests (requires services running) |
