<p align="center">
  <img src="https://oblako-public.s3.amazonaws.com/oblako_logo.png" width="320" alt="oblako">
</p>

<p align="center">
  <b>LocalStack simulates the API; oblako simulates the topology around a real engine.</b>
</p>

<p align="center">
  <a href="https://oblako-sdk.almostly.ai/">Documentation</a>
</p>

# oblako

oblako is a local AWS platform: run Bedrock, SageMaker, Redshift, Step Functions
and more on your laptop, no cloud required. You write the AWS code you already
write — **every service maps 1:1 to a `boto3` client** — and oblako runs it
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
are genuinely doing the work — **real behavior, simulated topology**.

## Services

Every service is the local counterpart of an AWS service, reached through its
normal `boto3` client (or native driver):

| AWS Service | Local replacement | How |
|---|---|---|
| Bedrock (LLMs) | Ollama (or OpenRouter) | boto3 `bedrock-runtime` invoke/converse; OpenRouter backend hits real models with your key |
| Bedrock (control plane) | oblako server | boto3 `bedrock`: foundation-model catalog + batch model-invocation jobs |
| Bedrock Knowledge Bases | OpenSearch | Vector search with k-NN |
| Bedrock AgentCore (Runtime) | bedrock-agentcore SDK | Local agent on the `/invocations` + `/ping` contract |
| SageMaker | SDK local mode | `instance_type="local"` trains in real Docker |
| Step Functions | aws-stepfunctions-local | Official AWS Docker image |
| Lambda | AWS SAM CLI (external) | `sam local invoke` — bring your own SAM CLI |
| S3 | S3Proxy | S3 API over the local filesystem |
| DynamoDB | dynamodb-local | Official AWS Docker image |
| Redshift (engine) | oblako image (PostgreSQL 16) | impersonates Redshift: redshift-connector natively, system tables, `SET query_group`, UDFs |
| Redshift (management API) | moto | boto3 `redshift` control plane: clusters, nodes, endpoints |
| Redshift Data API | oblako server | boto3 `redshift-data`, real SQL against the engine |
| Redshift ML | SageMaker local + plpython3u | `CREATE MODEL` trains in a container; predict UDF runs in-DB |
| RDS / Aurora | moto + PostgreSQL/MySQL | boto3 `rds` control plane + a real engine |
| RDS Data API | oblako server | boto3 `rds-data`: synchronous SQL + transactions |
| CloudFormation | oblako server | boto3 `cloudformation` (+ `aws cloudformation deploy` / `sam deploy`) → **real** oblako resources |
| Glue | Spark + S3Proxy | Spark jobs read/write oblako's S3 |
| AppConfig | oblako agent | Python reimplementation of the feature-flag / A/B split |

Full per-service guides, limitations, and the Python API are in the
**[documentation](https://oblako-sdk.almostly.ai/)**.

## Quick start

```bash
pip install oblako

oblako up                  # start all services
oblako pull qwen2.5:0.5b   # pull a model into the Bedrock (Ollama) engine
oblako dashboard           # web UI at http://localhost:8000
```

Your unmodified `boto3` code then hits the local services — no `endpoint_url`,
no config (the dashboard, notebook, and env helpers wire `AWS_ENDPOINT_URL_*`
for you).

## CLI

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
| `oblako test` / `oblako test-integration` | Run unit / integration tests |

Service names: `bedrock`, `opensearch`, `redshift`, `rds`, `moto`, `s3`,
`dynamodb`, `stepfunctions` (`ollama` aliases `bedrock`; `aurora` aliases `rds`).

## Container runtimes

oblako isn't hard-wired to Docker — every service runs through a pluggable
backend, selected with `OBLAKO_CONTAINER_BACKEND`: `docker` (default), `podman`,
`colima`, `kubernetes`, or Apple's `container` (`apple`). See the
[runtimes guide](https://oblako-sdk.almostly.ai/runtimes.html).

## Documentation

The full docs — architecture, every service with its limitations, the dashboard,
container runtimes, and the Python API reference — live at
**[oblako-sdk.almostly.ai](https://oblako-sdk.almostly.ai/)**.
