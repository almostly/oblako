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
- **RDS / Aurora** → a real PostgreSQL engine behind moto's control plane
- **Step Functions** → Amazon's `aws-stepfunctions-local`
- **Bedrock** → Ollama (or OpenRouter for real frontier models)
- **SageMaker** → the SDK's local mode (real Docker training containers)

Control planes that have no local engine (cluster/instance metadata for
Redshift, RDS, IAM, EC2, Lambda) are served by **moto**, so `describe_*` calls
behave; the data plane runs against the real engine.

## Caddy ties the topology together

A **Caddy** reverse proxy fronts services with AWS-style vanity hostnames
(e.g. `mlflow.oblako.aws` → the local MLflow), so the local topology *looks*
like AWS, not like a pile of `localhost:PORT` endpoints.

## What this buys you

- Your SQL actually executes (dbt-redshift runs; `SELECT` returns real rows).
- Your boto3 code is unchanged between local and cloud.
- You can develop and test end-to-end offline.

See [Services](services.md) for what each service supports and where it diverges
from AWS, and the [Python API](api.md) for the `oblako.services` reference.
