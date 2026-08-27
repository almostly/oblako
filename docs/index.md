# oblako

> **oblako simulates the topology around real local engines. Real behavior, not a mock.**

oblako is a local AWS platform: run Bedrock, SageMaker, Redshift, Step Functions
and more on your laptop, no cloud required. You write the AWS code you already
write, every service maps 1:1 to a `boto3` client, and oblako runs it against
**real local engines** wired into an AWS-shaped topology.

```{figure} _static/diagrams/routing.svg
:alt: boto3 to localhost:PORT to a real engine, with no proxy in the path
:width: 100%

Your `boto3` code, unmodified: one client, one fixed local port, one real engine.
```

```python
from oblako.services import RedshiftService

rs = RedshiftService().get_client()   # this is just boto3.client("redshift")
rs.describe_clusters()
```

## What runs locally

Each service is a real engine on a fixed local port, reached by the same `boto3`
client you'd use in the cloud.

```{figure} _static/diagrams/containers.svg
:alt: Containers and compute — ECS, Fargate, EKS, ECR, Lambda, Step Functions
:width: 100%

Containers & compute: real containers.
```

```{figure} _static/diagrams/ai-ml.svg
:alt: AI/ML — SageMaker, Bedrock, DynamoDB vectors, OpenSearch
:width: 100%

AI / ML: SageMaker local mode, Bedrock via Ollama, vector search.
```

```{figure} _static/diagrams/data.svg
:alt: Data and analytics — S3, Glue, Redshift, Athena, Kinesis, Firehose, RDS, DynamoDB
:width: 100%

Data & analytics: real engines.
```

See [Architecture](architecture.md) for how the topology fits together, including
where Caddy sits.

```{toctree}
:maxdepth: 2
:caption: Documentation

quickstart
architecture
services
dashboard
runtimes
api
license
```
