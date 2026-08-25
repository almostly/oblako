# Third-party licenses

oblako itself is licensed under the Apache License 2.0 (see `LICENSE`). It is a
thin orchestration layer: its value is the topology it wires around real engines,
not the engines themselves. This file inventories the third-party software oblako
depends on and clarifies what it does and does not redistribute.

## How oblako relates to these components

- **Python packages** are ordinary runtime dependencies, resolved by `pip`/`uv`
  from PyPI. oblako does not vendor or modify them.
- **Container images** are pulled by the user at runtime from their public
  registries (Docker Hub, etc.). oblako does not host, repackage, or redistribute
  these images; it runs them as separate processes and talks to them over the
  network. This is aggregation/use, not distribution, so their licenses attach to
  the user's own pulled copies, not to the oblako source you obtained.
- A few images are **built locally on the user's machine** from a public base
  (e.g. the Redshift engine builds `FROM postgres:16`). The oblako Dockerfiles
  that describe those builds are Apache-2.0; the resulting image inherits the
  license of whatever base it is built from (see the Citus note below).

Nothing here is legal advice. Before any **commercial** distribution, have counsel
review the two flagged components (Citus, DynamoDB Local).

## Python dependencies

| Package | License |
| --- | --- |
| boto3 / botocore | Apache-2.0 |
| docker (SDK) | Apache-2.0 |
| fastapi | MIT |
| httpx | BSD-3-Clause |
| pyyaml | MIT |
| starlette | BSD-3-Clause |
| uvicorn | BSD-3-Clause |
| psycopg2-binary | LGPL-3.0-or-later (with binary exceptions) |
| psycopg (v3) | LGPL-3.0-or-later |

The two `psycopg` drivers are LGPL. They are used unmodified as dynamically
imported libraries, which does not place any copyleft obligation on oblako's own
Apache-2.0 code. If you ever ship oblako as a single frozen binary, keep the
LGPL components replaceable (dynamically linked) to stay compliant.

## Orchestrated container images

| Image | Role in oblako | License |
| --- | --- | --- |
| `motoserver/moto` | AWS control planes (IAM, SNS, SQS, ECS, EKS, ...) | Apache-2.0 |
| `andrewgaul/s3proxy` | S3 data plane | Apache-2.0 |
| `trinodb/trino` | Athena query engine | Apache-2.0 |
| `postgres:16` | base for the Redshift single-node engine | PostgreSQL License (permissive) |
| `ollama/ollama` | Bedrock runtime backend | MIT |
| MLflow (built image) | tracking server | Apache-2.0 |
| kinesalite | Kinesis Streams | MIT |
| `shogo82148/lambda-*` | Lambda runtime images | MIT |
| `python:3.11-slim` / `python:3.12-slim` | base for oblako's built images | PSF + Debian (permissive) |
| JupyterLab (pip-installed in the notebook image) | notebook UI | BSD-3-Clause |
| **`amazon/dynamodb-local`** | DynamoDB data plane | **Amazon Software License** (see below) |
| **`citusdata/citus:12.1`** | Redshift MPP cluster variant | **AGPL-3.0** (see below) |

## Flagged components (review before selling)

### Citus (AGPL-3.0)

The **optional** Redshift MPP cluster image (`oblako/images/redshift-cluster`)
builds `FROM citusdata/citus:12.1`, which is AGPL-3.0. Consequences:

- oblako's **source** stays Apache-2.0. A Dockerfile referencing an AGPL base
  does not make oblako's Python AGPL.
- The **built cluster image** is a derivative of Citus and is therefore AGPL-3.0.
  AGPL's network clause means anyone who *offers that image as a network service*
  must offer its source. For a user running it locally, this is a non-issue.
- **For a commercial/hosted oblako edition:** do not build, host, or ship the
  Citus-based image as part of the paid service. Keep it strictly opt-in and
  user-built, or replace the MPP path with a non-AGPL engine.

The default single-node Redshift engine (`FROM postgres:16`) is **not** affected
and carries no copyleft.

### DynamoDB Local (Amazon Software License)

`amazon/dynamodb-local` is under the Amazon Software License: free to use, but
redistribution and use outside AWS-related development are restricted. oblako
only pulls and runs it locally, which is permitted. Do **not** bundle or
redistribute this image in a paid product without checking the ASL terms.

## Clean-room note

oblako reimplements AWS API *behavior* from public documentation and observed
responses. It does not copy source from LocalStack or any AWS SDK internals
(a source grep for `localstack` in `oblako/` returns zero matches). Keep new work
clean-room to preserve the Apache-2.0 licensing of the core.
