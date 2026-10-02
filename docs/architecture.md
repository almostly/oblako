# Architecture

oblako is built on one idea: **real behavior, simulated topology**. Where a mock
returns canned API responses, oblako runs the *actual* engine an AWS service is
built on and wires it into an AWS-shaped local topology, so your code, your SQL,
and your models all run for real.

## Every service maps 1:1 to boto3

Each oblako service is the local counterpart of an AWS service, reached through
the **same `boto3` client you'd use in the cloud**. There is no oblako-specific
SDK to learn:

```python
from oblako.services import Oblako

o = Oblako()
o.s3.get_client()         # boto3.client("s3")        -> S3Proxy
o.dynamodb.get_client()   # boto3.client("dynamodb")  -> DynamoDB Local
o.redshift.get_client()   # boto3.client("redshift")  -> moto control plane
o.redshift.connect()      # psycopg2 / redshift-connector -> the real engine
```

In a notebook or with the endpoint env vars set, unmodified `boto3.client("s3")`
transparently hits the local service, no `endpoint_url`, no config.

## Real engines, not mocks

oblako prefers running the real thing over emulating it:

- **S3** → S3Proxy over the local filesystem
- **DynamoDB** → Amazon's DynamoDB Local
- **Redshift** → a PostgreSQL 16 image that *impersonates* Redshift (accepts
  `redshift-connector` natively, reports `server_version 8.0.2`)
- **RDS / Aurora** → real PostgreSQL: one container per standalone DB instance
  (read replicas are streaming standbys), behind an RDS API proxy over moto
- **Step Functions** → Amazon's `aws-stepfunctions-local`
- **Bedrock** → Ollama (or OpenRouter for real frontier models)
- **SageMaker** → oblako's own engine, running jobs and endpoints in real Docker
  containers (plus account-free stubs for the SDK v3 local modes)

Control planes that have no local engine (cluster/instance metadata for
Redshift, RDS, IAM, EC2, Lambda) are served by **moto**, so `describe_*` calls
behave; the data plane runs against the real engine.

The three families oblako covers, each service on its own fixed local port:

```{figure} _static/diagrams/containers.svg
:alt: Containers and compute — ECS, Fargate, EKS, ECR, Lambda, Step Functions
:width: 100%

Containers & compute: real containers (ECS, Fargate, EKS, ECR, Lambda, Step Functions).
```

```{figure} _static/diagrams/ai-ml.svg
:alt: AI/ML — SageMaker, Bedrock, DynamoDB vectors, OpenSearch
:width: 100%

AI / ML: SageMaker in local Docker, Bedrock via Ollama, DynamoDB vector search, OpenSearch.
```

```{figure} _static/diagrams/data.svg
:alt: Data and analytics — S3, Glue, Redshift, Athena, Kinesis, Firehose, RDS, DynamoDB
:width: 100%

Data & analytics: real engines (S3, Glue, Redshift, Athena, Kinesis, Firehose, RDS, DynamoDB).
```

## How your code reaches a service

`AWS_ENDPOINT_URL_*` (or an explicit `endpoint_url=`) points `boto3` at
`localhost:PORT`, and the service publishes that port. One client, one fixed local
port, one real engine. Behind the port is one of four things:

- **The engine itself**, when it already speaks the AWS API: DynamoDB Local on
  :8001, kinesalite on :4567.
- **A thin front that adds what the engine lacks.** S3's :9000 is a stock nginx:
  it passes ordinary requests to S3Proxy on :9001, sends tagging and Inventory to
  the S3 extensions engine on :8020, and logs completed writes so event
  notifications fire. Redshift's :5439 is a wire proxy inside the Redshift image:
  it terminates TLS, rewrites Redshift-only SQL (`DISTKEY`, `SORTKEY`, `SUPER`
  navigation, ...) and bridges `COPY`/`UNLOAD` to S3, in front of PostgreSQL.
- **A Python engine of oblako's own**, for APIs no engine speaks: Athena (:8009,
  queries run on Trino), the Glue Data Catalog (:8486), S3 Tables, S3 Vectors,
  Firehose, EventBridge and the Data APIs.
- **moto** (:5500), for control planes with no engine behind them.

```{figure} _static/diagrams/routing.svg
:alt: boto3 to localhost:PORT to a real engine, with no proxy in the path
:width: 100%

boto3 / AWS CLI → `localhost:PORT` → the real engine (directly for most services).
```

**Caddy** sits *beside* that path, not in front of it, for two specific jobs:

- **Vanity hostnames**: it gives the local **MLflow** an AWS-shaped URL
  (`mlflow-oblako.<account>.<region>.experiments.sagemaker.aws`), so a
  SageMaker-managed tracking server and the local one look identical to your code.
- **Load balancing**: each **ELBv2 Application Load Balancer** is its own Caddy
  container, round-robining across its registered targets; the LB's `DNSName`
  resolves to `localhost:<listener-port>`.

Caddy reaches upstreams on the host via the `host.docker.internal` gateway alias
(the vmnet gateway on the Apple `container` backend), independent of any Docker
network. Ordinary service traffic (S3, Redshift, DynamoDB, …) never passes through
Caddy.

```{figure} _static/diagrams/caddy.svg
:alt: Caddy's two jobs — MLflow vanity host and per-ALB load balancer
:width: 100%

Caddy's two jobs beside the fixed-port path: the MLflow vanity host, and one Caddy per ELBv2 load balancer.
```

## What this buys you

- Your SQL actually executes (dbt-redshift runs; `SELECT` returns real rows).
- Your boto3 code is unchanged between local and cloud.
- You can develop and test end-to-end offline.

See [Services](services.md) for what each service supports and where it diverges
from AWS, and the [Python API](api.md) for the `oblako.services` reference.
