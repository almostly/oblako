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
`dynamodb`, `stepfunctions` (`ollama` aliases `bedrock`; `aurora` aliases `rds`).

## CLI reference

| Command | Description |
|---|---|
| `oblako up [service]` | Start all services (or a specific one) |
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
