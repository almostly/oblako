# Quick start

## Prerequisites

- **A container runtime**: oblako runs services as local containers. Docker is the
  default; Podman, Colima, Kubernetes and Apple's `container` also work (see
  [Container runtimes](runtimes.md)).
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
oblako configure           # write the `oblako` AWS profile
export AWS_PROFILE=oblako  # point boto3 and the AWS CLI at oblako
oblako pull qwen2.5:0.5b   # pull a model into the Bedrock (Ollama) engine
oblako dashboard           # web UI at http://localhost:8000
```

`oblako configure` writes a profile whose per-service endpoints are oblako's. With
it selected, boto3 and the AWS CLI reach the local services; select another
profile and the same code talks to AWS.

## Your AWS code just works

Each service is reached through its normal `boto3` client. With the `oblako`
profile selected (or in `oblako notebook`, which sets the endpoints itself),
unmodified `boto3` transparently hits the local service, no `endpoint_url`:

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
runs a PostgreSQL container per DB instance), `redshift` starts the Redshift API on
:8015, which runs multi-node clusters, and `dynamodb` starts the proxy on :8007
that adds vector search and tags. A bare `oblako up` starts all of these
except `iceberg` and `trino`.

The API engines that oblako runs in Python rather than in a container start the
same way, each on its canonical port, so plain boto3 or the AWS CLI can reach
them without any oblako code in the client: `s3vectors`, `s3tables`, `athena`,
`firehose`, `eventbridge`, `appconfig`, `sagemaker`, `glue`,
`dynamodb-vectors`, `redshift-data`, `redshift-control`, `rds-data`, `rds-control`,
`mwaa`, `ecs`, `bedrock-runtime`, `cloudformation`, `ecs-metadata`, `s3-ext`
(`oblako up s3` starts `s3-ext` itself).

```bash
oblako up s3vectors         # S3 Vectors on :8012, in the background
oblako logs s3vectors       # its log (~/.oblako/logs/s3vectors.log)
oblako down s3vectors
```

(Code that uses oblako's Python API starts these engines on demand.)

### When a port is taken

Every service has a fixed port (`oblako/ports.py`), so code and profiles can rely on
it. If another program already holds one, a local PostgreSQL on 5432 for example,
`oblako up` stops with an error that names the port. Stop the other program, or
move oblako's service with `OBLAKO_PORT_<NAME>`, where `<NAME>` is the port's name
in `oblako/ports.py`:

```bash
export OBLAKO_PORT_RDS_PG=5433   # in your shell profile, so every oblako command sees it
oblako up rds
oblako configure                 # rewrites the profile's endpoints with the new port
```

Every oblako process reads the variable, so the service, the engines and the
profile agree. An unknown name or a value that is not a port fails at once.

### Network exposure

oblako listens on your machine only. Its services use fixed local credentials, so
their containers publish their ports on `127.0.0.1`, and `oblako dashboard` and
`oblako notebook`, which have no login and can run code against your services,
listen there too. Containers still reach each other through the host
(`host.docker.internal`): Docker Desktop routes that to the host's loopback, and on
other engines (native Linux Docker, Colima, Podman) the ports also listen on the
docker bridge's gateway, an address internal to the machine.

To share oblako with other machines on purpose, such as a server your team uses,
set `OBLAKO_BIND_ADDRESS` (one address, or a comma-separated list) before `oblako
up`, and keep that network trusted:

```bash
export OBLAKO_BIND_ADDRESS=0.0.0.0
```

Containers keep the addresses they were created with; `oblako down` and `oblako up`
recreate them.

If an Ollama already runs on port 11434, installed on your machine say, `oblako up`
uses it instead of starting its own container, and `oblako down` leaves it alone.

## CLI reference

| Command | Description |
|---|---|
| `oblako up [service] [--timeout S]` | Start all services, or one (a container service like `s3`, or an API engine like `s3vectors`), and wait up to `S` seconds (default 120) for each to be ready; exits 1 if any is not, so a CI step fails there |
| `oblako down [service]` | Stop all services (or a specific one) |
| `oblako status` | Show service status |
| `oblako configure [--profile NAME]` | Write an AWS profile (default `oblako`) whose per-service endpoints are oblako's, with generated keys, to `~/.aws/config` and `~/.aws/credentials`; then `export AWS_PROFILE=oblako` points boto3 and the AWS CLI at oblako, and another profile at AWS. Services without their own entry go to moto, so no call leaves oblako. Other profiles are left as they are |
| `oblako dashboard [-p PORT] [--host ADDR]` | Start the web dashboard (default: 8000), on `127.0.0.1` only unless `--host` says otherwise; it has no login |
| `oblako notebook [-p PORT] [--dir DIR]` | Launch JupyterLab wired to oblako (default: 8888), with its workspace in `DIR` (default: `~/.oblako/notebooks`) |
| `oblako redshift-data [-p PORT]` | Start the Redshift Data API server (default: 8002) |
| `oblako bedrock-runtime [-p PORT]` | Start the Bedrock Runtime server (default: 8004) |
| `oblako rds-data [-p PORT]` | Start the RDS Data API server (default: 8006) |
| `oblako cloudformation [-p PORT]` | Start the CloudFormation server (default: 8017) |
| `oblako agentcore run <file> [-p PORT]` | Run a local AgentCore agent (default: 8080) |
| `oblako agentcore invoke <json> [-p PORT]` | Send a JSON payload to a running AgentCore agent |
| `oblako trust [--python EXE]` | Trust the local Redshift's TLS certificate in a venv's `redshift-connector`, so it and dbt can use `sslmode=verify-ca` |
| `oblako logs <service> [-n LINES]` | Show the last `LINES` lines (default 50) of a service's logs |
| `oblako pull [model]` | Pull a model into the engine (default: `qwen2.5:0.5b`) |
| `oblako models` | List available Ollama models |
| `oblako test` | Run unit tests |
| `oblako test-integration` | Run integration tests (requires services running) |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OBLAKO_CONTAINER_BACKEND` | `docker` | Container runtime: `docker`, `podman`, `colima`, `kubernetes` or `apple` (see [Container runtimes](runtimes.md)) |
| `OBLAKO_K8S_NAMESPACE` | `oblako` | Namespace for the Kubernetes backend |
| `OBLAKO_BIND_ADDRESS` | `127.0.0.1` | Address, or comma-separated addresses, the services' ports listen on |
| `OBLAKO_PORT_<NAME>` | see `oblako/ports.py` | Move one service off its default port |
| `OBLAKO_REGION` | `us-east-1` | Region oblako reports in ARNs and endpoints |
| `OBLAKO_ACCOUNT_ID` | `123456789012` | Account ID oblako reports in ARNs |
| `OBLAKO_BEDROCK_BACKEND` | `ollama` | Bedrock runtime backend: `ollama`, or `openrouter` (needs `OPENROUTER_API_KEY`) |
| `OBLAKO_OLLAMA_URL` | `http://localhost:11434` | Ollama server the Bedrock runtime uses |
| `OBLAKO_NOTEBOOK_DIR` | `~/.oblako/notebooks` | Workspace for `oblako notebook` |
