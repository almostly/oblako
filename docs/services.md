# Services

Every service is reached through its normal `boto3` client (or native driver).
This page lists what each one provides and where it diverges from AWS.

## AI / ML

| Service | Description | Limitations |
|---|---|---|
| **Bedrock (runtime)** | `bedrock-runtime` `invoke_model` / `converse`, translated to **Ollama** (offline) or **OpenRouter** (real frontier models with your key). | Local model quality ≠ frontier unless using the OpenRouter backend. |
| **Bedrock (control plane)** | `bedrock`: foundation-model catalog + batch model-invocation jobs. | Catalog is curated, not the full AWS list. |
| **Bedrock embeddings** | Ollama + `nomic-embed-text` for RAG vectors. | Embedding dims/model differ from Titan/Cohere. |
| **Bedrock Agents** | Agent loop with local tool calls (Ollama + SAM local). | No managed orchestration; tool-calling quality is model-dependent. |
| **Bedrock AgentCore (Runtime)** | Local agent on the `/invocations` + `/ping` contract via the `bedrock-agentcore` SDK. | Runtime only, Gateway/Memory/Identity are managed-only. |
| **Bedrock Knowledge Bases** | Vector search with k-NN over **OpenSearch**. | Retrieval only; no managed ingestion pipeline. |
| **SageMaker** | SDK **local mode**: real Docker training containers (`instance_type="local"`). | Needs `oblako[sagemaker]` (v2 SDK) + Docker; local mode only. |
| **SageMaker MLflow** | Managed MLflow tracking-server container (SigV4 auth, boto3-style creds). | Needs `oblako[mlflow]`. |

## Storage & databases

| Service | Description | Limitations |
|---|---|---|
| **S3** | S3 API over the local filesystem (S3Proxy). | No flexible-checksum / `aws-chunked`; oblako sets checksum calc `when_required`. |
| **S3 Tables / Iceberg** | Iceberg REST catalog with tables on S3Proxy. | Iceberg-on-S3; not the managed S3 Tables maintenance (compaction etc.). |
| **DynamoDB** | Amazon's DynamoDB Local. | Single local instance; no Streams→Lambda wiring. |
| **Kinesis** | Kinesis Data Streams via kinesalite (`saidsef/aws-kinesis-local`). | Streams only; no Firehose / Managed Flink. |
| **Redshift** | PostgreSQL 16 impersonating Redshift; `redshift-connector`/dbt connect natively. A bundled proxy tolerates physical DDL (`DISTKEY`/`SORTKEY`/`ENCODE`, `varchar(max)`) and terminates TLS. | Row-store, not columnar; late-binding views / `SUPER` unsupported; Python UDFs are Python 3. |
| **Redshift (control plane)** | `redshift` clusters/nodes/endpoints/snapshots via moto. | Metadata only, the cluster endpoint isn't the queryable engine. |
| **Redshift Data API** | `redshift-data`; SQL executes for real against the engine. | Statement results buffered in memory. |
| **Redshift ML** | `CREATE MODEL` trains in a SageMaker local container; predict UDF runs in-DB. | Needs `oblako[sagemaker]` + Docker; pure-Python predict UDF. |
| **RDS / Aurora** | moto control plane + a real PostgreSQL engine. | Engine is PostgreSQL regardless of the requested engine type. |
| **RDS Data API** | `rds-data`: synchronous SQL + transactions against the engine. | PostgreSQL semantics. |

## Analytics

| Service | Description | Limitations |
|---|---|---|
| **Athena** | Athena-style SQL over the Iceberg REST catalog (= S3 Tables), via **Trino**. | Trino SQL dialect, not Athena/Presto-exact; no workgroups/federation. |
| **Glue (jobs)** | PySpark jobs in the official `amazon/aws-glue-libs:5` image (per-job container). | ~5 GB image; sequential workflows only (no full DAGs/crawlers). |
| **Glue Data Catalog** | boto3 `glue` client bridged to the Iceberg REST catalog. | Catalog operations over Iceberg; not the full Glue catalog surface. |

## Orchestration & compute

| Service | Description | Limitations |
|---|---|---|
| **Step Functions** | Amazon's `aws-stepfunctions-local`. | Lambda-backed states need a running SAM CLI. |
| **Lambda** | Control plane + **real Docker-based invocation**. | x86_64 + python3.12 runtime image; SAM CLI for the dev-loop. |
| **ECS / Fargate** | `ecs` control plane (moto) + **each task is a real container**; `run_task`/`create_service` launch the image, wired to oblako's endpoints and published on a host port. | Fargate launch type; no continuous reconciliation/autoscaling; per-task compute needs a Docker socket. |
| **ELBv2 (ALB)** | `elbv2` control plane (moto) + **each load balancer is a real Caddy reverse proxy** that round-robins to the targets with the target group's health check; `DNSName` resolves to `localhost:<port>`. | ALB (HTTP); no listener-rule path routing yet; NLB/GWLB not modelled. |
| **API Gateway** | External, AWS SAM CLI (`sam local start-api`) routing HTTP to your functions. | oblako doesn't manage it; bring your own SAM CLI. |

## Management & control planes

| Service | Description | Limitations |
|---|---|---|
| **CloudFormation** | `cloudformation` (+ `aws cloudformation deploy` / `sam deploy`) provisions **real** oblako resources, including a full ECS Fargate + ALB stack. | Subset of resource types (S3, DynamoDB, Redshift, RDS, ECS, ELBv2, …). |
| **IAM / STS** | moto control plane + oblako's policy evaluator. | Policy evaluation is a best-effort reimplementation. |
| **EC2** | moto control plane + real container-backed instances. | `describe_*` fidelity; instances are containers, not VMs. |
| **OpenSearch** | OpenSearch single-node (Knowledge Bases / RAG). | Security plugin disabled for local use. |
| **AppConfig** | Python reimplementation (control + data plane + rule evaluation). | Reimplementation, not the AWS engine. |

---

The deep dives below cover the services with the most local-specific behavior.

## Redshift

A PostgreSQL 16 image (`public.ecr.aws/oblako/redshift-local`, mirrored on Docker
Hub as `deburky/redshift-local`) that *impersonates* Amazon
Redshift. A small `shared_preload` extension accepts the Redshift-only startup
parameters Amazon's `redshift-connector` driver sends and reports
`server_version 8.0.2`, so the driver, and dbt-redshift, connect **natively**
(no wire shim for the handshake). A thin proxy bundled in the same container
makes the engine tolerate Redshift physical DDL (see below). It ships the
Redshift system tables, `SET query_group`, JSON/scalar
UDFs (`json_extract_path_text`, `json_array_length`, `median`, `decode`), and the
Redshift **date/time functions** PostgreSQL lacks: `getdate`, `sysdate`,
`dateadd`, `datediff` (boundary-crossing semantics), `add_months`, `last_day`,
`months_between`, `trunc(timestamp)`, `convert_timezone`. The date parts must be
quoted (`dateadd('day', 7, ts)`), as most SQL generators emit them. It also adds
the Redshift **catalog views** BI tools and dbt query for metadata, mapped onto
PostgreSQL's catalogs: `pg_table_def`, `svv_tables`, `svv_columns`,
`svv_table_info`, and (empty) `svv_external_schemas` / `svv_external_tables` /
`svv_external_columns`.

**SQLAlchemy / Alembic.** The `sqlalchemy-redshift` dialect
(`redshift+redshift_connector://…`) reflects too: its introspection reads
Redshift-only catalog columns (`reldiststyle`, `attencodingtype`, …) and filters
by output-column aliases in `WHERE`, neither of which stock PostgreSQL has, so the
proxy answers them. `get_columns`, table autoload, `has_table`, and thus **Alembic
autogenerate** work against the engine (the driver is a client dependency, nothing
is added to the image).

```python
from oblako.services import RedshiftService
con = RedshiftService().connect()   # psycopg2 to the engine on 5439
```

dbt-redshift: a `type: redshift` profile pointed at `host: localhost`,
`port: 5439`. For verified TLS, run `oblako trust` once in the venv dbt uses, then
set `sslmode: verify-ca` (see TLS below).

**TLS.** The bundled proxy terminates SSL. The image ships a **fixed self-signed
cert** (`CN=localhost`), baked in so every container, `docker compose down -v`,
and fresh clone presents the *same* cert. That's what makes a pinned
`sslrootcert` or `oblako trust` stay valid instead of going stale after a volume
reset. It's a deliberate non-secret for local dev; override by mounting your own
keypair at `/etc/oblako-redshift`, or disable TLS with `OBLAKO_SSL=0`.

- **libpq clients** (psycopg2, and JDBC tools like Metabase) work out of the box
  with `sslmode=require` (encrypt), or `verify-full` with `sslrootcert` pointed at
  `oblako/images/redshift/certs/server.crt` (the same cert the container serves).
- **redshift_connector (dbt, awswrangler)** verifies only against a hardcoded
  Amazon CA bundle with no override, so it can't verify a local cert by default.
  Run **`oblako trust`** once, it appends the proxy's cert to that venv's
  redshift-connector bundle, then use `sslmode: verify-ca` for real, verified TLS
  (no `ssl=False`). Because the cert is fixed, one trust holds across recreates;
  re-run only after a `redshift-connector` reinstall (which restores the pristine
  bundle) or in a fresh venv / CI runner. That venv then also trusts the cert
  against real Redshift (harmless: it's a self-signed localhost cert). Without
  trust, use `sslmode: disable` locally.

`OBLAKO_SSL=0` turns TLS off entirely.

**Limitations**

- Redshift physical DDL (`DISTSTYLE`/`DISTKEY`/`SORTKEY`/`ENCODE`) is **accepted
  and ignored** (the bundled wire proxy strips it before the parser), and
  `varchar(max)` is rewritten to `text`, so awswrangler `to_sql`, dbt physical
  configs, and dlt's Redshift destination all work. It has no storage effect on
  the PostgreSQL engine. Late-binding views and `SUPER`/`VARBYTE` are still
  unsupported.
- It's a row-store PostgreSQL, not columnar: no Redshift-style column compression
  / zone maps / sort-key ordering. Query **results** are faithful; storage and
  performance characteristics are not.
- Python UDFs run as **Python 3** (real Redshift's are Python 2, which Amazon is
  sunsetting); `LANGUAGE plpythonu` is aliased to the Python 3 handler.
- It's a PostgreSQL engine underneath, no columnar storage, distribution, or
  the Redshift query planner.

## S3

S3 API backed by S3Proxy over the local filesystem.

**Limitations**

- S3Proxy doesn't implement the AWS SDK v2 default flexible checksums
  (`x-amz-checksum-*` over `aws-chunked`); oblako sets checksum calculation to
  `when_required` so uploads work.

## SageMaker

Runs the SageMaker SDK's **local mode**, `instance_type="local"` launches real
Docker training containers.

**Limitations**

- Requires `pip install 'oblako[sagemaker]'` (pinned to the v2 SDK, v3 dropped
  local mode) and Docker.
