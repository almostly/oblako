<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://oblako-public.s3.amazonaws.com/oblako_logo_dark.png?v=2">
    <img src="https://oblako-public.s3.amazonaws.com/oblako_logo.png?v=2" width="320" alt="oblako">
  </picture>
</p>

<p align="center">
  <b>oblako simulates the topology around real local engines. Real behavior, not a mock.</b>
</p>

<p align="center">
  <a href="https://oblako-sdk.almostly.ai/">Documentation</a>
</p>

# oblako

oblako is a local AWS platform: run Bedrock, SageMaker, Redshift, Step Functions
and more on your laptop, no cloud required. You write the AWS code you already
write, **every service maps 1:1 to a `boto3` client**, and oblako runs it
against **real local engines** wired into an AWS-shaped topology.

```python
import boto3

s3 = boto3.client("s3")          # -> S3Proxy, real S3 API over the filesystem
s3.create_bucket(Bucket="demo")

ddb = boto3.client("dynamodb")   # -> DynamoDB Local
ddb.list_tables()
```

The difference from a mock: a bucket really stores bytes, a `redshift-connector`
session really runs SQL, a SageMaker job really trains in a container. oblako
fakes the *topology* (clusters, endpoints, control planes) around engines that
are genuinely doing the work, **real behavior, simulated topology**.

## Services

Every service is the local counterpart of an AWS service, reached through its
normal `boto3` client (or native driver):

| AWS Service | Local replacement | How |
|---|---|---|
| Bedrock (LLMs) | Ollama (or OpenRouter) | boto3 `bedrock-runtime` invoke/converse; OpenRouter backend hits real models with your key |
| Bedrock (control plane) | oblako server | boto3 `bedrock`: foundation-model catalog + batch model-invocation jobs |
| Bedrock Knowledge Bases | OpenSearch | Vector search with k-NN |
| Bedrock AgentCore (Runtime) | bedrock-agentcore SDK | Local agent on the `/invocations` + `/ping` contract |
| SageMaker | oblako engine + SDK v3 local mode | boto3 `sagemaker` API runs jobs and endpoints in real Docker; `ModelTrainer` / `ModelBuilder` local mode runs with no account |
| Step Functions | aws-stepfunctions-local | Official AWS Docker image |
| Lambda | AWS SAM CLI (external) | `sam local invoke`, bring your own SAM CLI |
| ECS / Fargate | moto + container per task | `run_task`/`create_service` launch real containers; a Fargate+ALB CloudFormation stack deploys and serves locally |
| ELBv2 (ALB) | moto + Caddy proxy per LB | real reverse proxy round-robining to tasks with the target group health check; `DNSName` → `localhost:<port>` |
| S3 | S3Proxy | S3 API over the local filesystem |
| DynamoDB | dynamodb-local | Official AWS Docker image |
| Redshift (engine) | oblako image (PostgreSQL 16) | impersonates Redshift: redshift-connector natively, system tables, `SET query_group`, UDFs |
| Redshift (management API) | moto | boto3 `redshift` control plane: clusters, nodes, endpoints |
| Redshift Data API | oblako server | boto3 `redshift-data`, real SQL against the engine |
| Redshift ML | Training container + plpython3u | `CREATE MODEL` from any SQL client, trained asynchronously in a container; prediction function runs in-DB |
| RDS / Aurora | moto + PostgreSQL/MySQL | boto3 `rds` control plane + a real engine |
| RDS Data API | oblako server | boto3 `rds-data`: synchronous SQL + transactions |
| CloudFormation | oblako server | boto3 `cloudformation` (+ `aws cloudformation deploy` / `sam deploy`) → **real** oblako resources |
| Glue | Spark + S3Proxy | Spark jobs read/write oblako's S3 |
| AppConfig | oblako agent | Python reimplementation of the feature-flag / A/B split |

Full per-service guides, limitations, and the Python API are in the
**[documentation](https://oblako-sdk.almostly.ai/)**.

## Quick start

oblako needs Python 3.10 or later and a container runtime: Docker (the default),
or Podman, Colima or Apple's `container` (see below). Most services run as
containers; a few API engines run in Python without one.

```bash
pip install oblako         # or: uv tool install oblako

oblako up                  # start the services
oblako configure           # write the `oblako` AWS profile
export AWS_PROFILE=oblako  # point boto3 and the AWS CLI at oblako
oblako dashboard           # web UI at http://localhost:8000
```

Your unmodified `boto3` code and the AWS CLI then reach the local services, with
no `endpoint_url` in the code. Select another AWS profile and the same code talks
to AWS.

## CLI

| Command | Description |
|---|---|
| `oblako up [service] [--timeout S]` | Start all services, or one (a container service like `s3`, or an API engine like `s3vectors`), and wait up to `S` seconds (default 120) for each to be ready; exits 1 if any is not, so a CI step fails there |
| `oblako down [service]` | Stop all services (or a specific one) |
| `oblako status` | Show service status |
| `oblako configure [--profile NAME]` | Write an AWS profile (default `oblako`) whose per-service endpoints are oblako's, with generated keys, to `~/.aws/config` and `~/.aws/credentials`; then `export AWS_PROFILE=oblako` points boto3 and the AWS CLI at oblako, and another profile at AWS. Services without their own entry go to moto, so no call leaves oblako. Other profiles are left as they are |
| `oblako dashboard [-p PORT] [--host ADDR]` | Start the web dashboard (default: 8000), on `127.0.0.1` only unless `--host` says otherwise; it has no login |
| `oblako notebook [-p PORT]` | Launch JupyterLab wired to oblako (default: 8888) |
| `oblako redshift-data [-p PORT]` | Start the Redshift Data API server (default: 8002) |
| `oblako bedrock-runtime [-p PORT]` | Start the Bedrock Runtime server (default: 8004) |
| `oblako rds-data [-p PORT]` | Start the RDS Data API server (default: 8006) |
| `oblako cloudformation [-p PORT]` | Start the CloudFormation server (default: 8017) |
| `oblako agentcore run <file>` | Run a local AgentCore agent (default: 8080) |
| `oblako logs <service>` | Show logs for a service |
| `oblako pull [model]` | Pull a model into the engine (default: `qwen2.5:0.5b`) |
| `oblako test` / `oblako test-integration` | Run unit / integration tests |

Service names: `bedrock`, `opensearch`, `redshift`, `rds`, `moto`, `s3`,
`dynamodb`, `stepfunctions` (`ollama` aliases `bedrock`; `aurora` aliases `rds`).

## Container runtimes

oblako isn't hard-wired to Docker, every service runs through a pluggable
backend, selected with `OBLAKO_CONTAINER_BACKEND`: `docker` (default), `podman`,
`colima`, `kubernetes`, or Apple's `container` (`apple`). See the
[runtimes guide](https://oblako-sdk.almostly.ai/runtimes.html).

## Documentation

The full docs (architecture, every service with its limitations, the dashboard,
container runtimes, and the Python API reference) live at
**[oblako-sdk.almostly.ai](https://oblako-sdk.almostly.ai/)**.

## License

oblako is open core. This repository is the open-source edition, licensed under
the [Apache License 2.0](LICENSE); see [`NOTICE`](NOTICE). Commercial Pro and
hosted editions are offered separately under their own terms.

oblako orchestrates third-party engines (moto, S3Proxy, Trino, Postgres, and
others) that it pulls at runtime rather than redistributing; each keeps its own
license. Two of them (Citus, DynamoDB Local) carry terms that matter for
commercial use, inventoried in [`THIRD_PARTY_LICENSES.md`](https://github.com/almostly/oblako/blob/main/THIRD_PARTY_LICENSES.md).
