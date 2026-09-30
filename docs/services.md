# Services

Every service is reached through its normal `boto3` client (or native driver).
This page lists what each one provides and where it diverges from AWS.

## AI / ML

| Service | Description | Limitations |
|---|---|---|
| **Bedrock (runtime)** | `bedrock-runtime` `invoke_model` / `converse`, plus **streaming** (`invoke_model_with_response_stream`, `converse_stream`) with real Bedrock eventstream framing. Translated to **Ollama** (offline) or **OpenRouter** (real frontier models with your key). | Local model quality ≠ frontier unless using the OpenRouter backend. |
| **Bedrock (control plane)** | `bedrock`: foundation-model catalog + batch model-invocation jobs. | Catalog is curated, not the full AWS list. |
| **Bedrock Guardrails** | `bedrock` guardrail CRUD + `bedrock-runtime` `ApplyGuardrail`; enforces custom word filters and denied-topic policies, returns `GUARDRAIL_INTERVENED` with per-policy assessments. | Content-filter and PII (`sensitiveInformation`) policies are stored but not enforced; the managed profanity list is small. |
| **Bedrock embeddings** | Titan Embed v1/v2 and Cohere Embed v3 mapped to Ollama `nomic-embed-text`; responses shaped per model family. | Vectors are `nomic-embed-text`'s, not the native Titan/Cohere dimensions; OpenRouter backend has no embedding support. |
| **Bedrock Agents** | Agent loop with local tool calls (Ollama + SAM local). | No managed orchestration; tool-calling quality is model-dependent. |
| **Bedrock AgentCore (Runtime)** | Local agent on the `/invocations` + `/ping` contract via the `bedrock-agentcore` SDK. | Runtime only, Gateway/Memory/Identity are managed-only. |
| **Bedrock Knowledge Bases** | Vector search with k-NN over **OpenSearch**. | Retrieval only; no managed ingestion pipeline. |
| **SageMaker** | Own Docker engine on the real `/opt/ml` contract (channels copied in, `model.tar.gz` collected, container polled on `/ping` and invoked on `:8080`). Covers training, HPO/AMT tuning, processing, batch transform, real-time + **async** (+ SNS) + **multi-model** endpoints, Studio domains/user-profiles, Feature Store, Model Monitor + data capture, and Model Registry. | Needs `oblako[sagemaker]` (v3 SDK) + Docker. Compute is local containers, not managed instances (`instance_type` is cosmetic); Feature Store online store is in-memory; Model Monitor is a one-shot violations report; HPO search is TPE/random, not AWS's Bayesian tuner; serverless config is echoed, not a distinct runtime. |
| **SageMaker MLflow** | Managed MLflow tracking-server container (SigV4 auth, boto3-style creds); the `…:mlflow-tracking-server/…` ARN resolves to the local server. | Needs `oblako[mlflow]`. |

## Storage & databases

| Service | Description | Limitations |
|---|---|---|
| **S3** | S3 API over the local filesystem (S3Proxy). | No flexible-checksum / `aws-chunked`; oblako sets checksum calc `when_required`. |
| **S3 Tables** | `s3tables` control plane (table buckets, namespaces, tables, `GetTableMetadataLocation`) mapped onto the local **Iceberg REST catalog**; `CreateTable` writes real Iceberg metadata, so the tables are queryable by Athena / Trino / pyiceberg. | Single-warehouse catalog: a table bucket + namespace map to a `[bucket, namespace]` Iceberg namespace prefix; managed maintenance (compaction, snapshot expiry) is not modelled. |
| **S3 Vectors** | `s3vectors`: vector buckets → indexes (dimension + distance metric) → `PutVectors` / `QueryVectors` (k-NN) with Mongo-style metadata filters. Fed by Bedrock embeddings. | Brute-force k-NN (cosine / euclidean), not ANN; vectors are in-memory (not persisted across restart). |
| **DynamoDB** | Amazon's DynamoDB Local, with a proxy that adds native **vector search**: `VectorIndexes` on `CreateTable`/`UpdateTable` + `SearchVectors` (k-NN). | Single local instance; no Streams→Lambda wiring. `SearchVectors` is brute-force KNN (full scan), not ANN; the vector index isn't persisted across restart. |
| **Kinesis** | Kinesis Data Streams via kinesalite (`saidsef/aws-kinesis-local`). | Streams only; no Managed Flink. (Firehose is a separate service, see Analytics.) |
| **Redshift** | PostgreSQL 16 impersonating Redshift; `redshift-connector`/dbt connect natively. A bundled proxy tolerates physical DDL (`DISTKEY`/`SORTKEY`/`ENCODE`, `varchar(max)`), terminates TLS, and bridges `COPY`/`UNLOAD` to/from `s3://` (Parquet, CSV, and delimited text) so awswrangler, dbt, and Feast load/unload for real. `SUPER` (jsonb-backed) with PartiQL navigation (`data.a.b`, `data['a'][0]`), `LISTAGG` (→ `string_agg`), `PIVOT`/`UNPIVOT` (→ standard SQL), unquoted dateparts (`DATEADD(month, 1, d)`), and native JSON functions are supported. | Row-store, not columnar; late-binding views unsupported; Python UDFs are Python 3; the S3 bridge covers Parquet/CSV/text (not JSON/AVRO/ORC); SUPER dot-navigation yields text (numeric compares need a cast); PIVOT/UNPIVOT need a subquery source (a bare table has no schema in the proxy). |
| **Redshift (control plane)** | `redshift` clusters/nodes/endpoints/snapshots via moto. | Metadata only, the cluster endpoint isn't the queryable engine. |
| **Redshift Data API** | `redshift-data`; SQL executes for real against the engine (through the same proxy, so its `COPY`/`UNLOAD` reach S3 too). Feast's Redshift offline store works end to end. | Statement results buffered in memory. |
| **Redshift ML** | `CREATE MODEL` / `SHOW MODEL` / `DROP MODEL` from any client (psycopg, DBeaver, dbt, the Data API), asynchronous like Redshift: the model trains in a container and `svv_ml_model_info` moves from `TRAINING` to `Model is Ready`; the prediction function runs in-DB. | Needs Docker (the engine mounts `/var/run/docker.sock`); numeric features only (`PREPROCESSORS 'none'`); pure-Python prediction function. |
| **RDS / Aurora** | moto control plane + a real PostgreSQL engine. | Engine is PostgreSQL regardless of the requested engine type. |
| **RDS Data API** | `rds-data`: synchronous SQL + transactions against the engine. | PostgreSQL semantics. |

## Analytics

| Service | Description | Limitations |
|---|---|---|
| **Athena** | The real boto3 `athena` API (`StartQueryExecution`, `GetQueryExecution`, `GetQueryResults`, `StopQueryExecution`) executed via **Trino**, with results written to the S3 `OutputLocation`. | Trino SQL dialect, not Athena/Presto-exact; `StopQueryExecution` is best-effort (Trino runs to completion); no workgroups/federation. |
| **Firehose** | `firehose` delivery streams: `DirectPut` and `KinesisStreamAsSource` sources, buffered and flushed to **S3** or **Redshift** (S3 staging + `COPY`). | Only S3 and Redshift destinations; buffering floors aren't enforced. |
| **Glue (jobs)** | PySpark jobs in the official `amazon/aws-glue-libs:5` image (per-job container). | ~5 GB image; sequential workflows only (no full DAGs/crawlers). |
| **Glue Data Catalog** | boto3 `glue` client bridged to the Iceberg REST catalog. | Catalog operations over Iceberg; not the full Glue catalog surface. |

## Orchestration & compute

| Service | Description | Limitations |
|---|---|---|
| **Step Functions** | Amazon's `aws-stepfunctions-local`. | Lambda-backed states need a running SAM CLI. |
| **Lambda** | Control plane + **real Docker-based invocation**. | x86_64 + python3.12 runtime image; SAM CLI for the dev-loop. |
| **ECS / Fargate** | `ecs` control plane (moto) + **each task is a real container**; `run_task`/`create_service` launch the image, wired to oblako's endpoints and published on a host port. Tasks also get the **ECS task metadata endpoint** (`ECS_CONTAINER_METADATA_URI[_V4]`) that AWS containers read. | Fargate launch type; no continuous reconciliation/autoscaling; per-task compute needs a Docker socket. |
| **EKS** | `eks` control plane (moto): create/describe/list clusters. | Control-plane metadata only; no real Kubernetes data plane (for local k8s, use the Kubernetes container backend). |
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
| **EventBridge** | `events` control plane (moto) + a proxy that actually **fires rule targets**: it delivers to **Redshift Data** targets (running `ExecuteStatement`) and fires **scheduled** `rate(...)` rules on cadence. | moto already delivers SQS/SNS/Lambda targets natively; the proxy adds only Redshift Data + scheduling. |
| **Common services (moto)** | Surfaced as-is via the moto container so unmodified boto3 works: `sns`, `sqs`, `sts`, `secretsmanager`, `ssm`, `kms`, `cloudwatch` (metrics), `logs` (CloudWatch Logs), `ecr`. | moto's control-plane fidelity; no data-plane behavior beyond what moto implements. |

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

**Access management as code.** Redshift tools that manage users, groups, and
privileges declaratively run against the engine too.
[redtape](https://github.com/tomasfarias/redtape) (MIT) introspects a forked
`pg_catalog` plus Redshift-only views and functions; the image answers the pieces
PostgreSQL lacks (`pg_user.usecatupd`, `svv_external_schemas.eskind`, `like_escape`,
the data-sharing / external-schema set-functions) and owns `public` by a real user
(PostgreSQL 15+ owns it by `pg_database_owner`, a role Redshift has no concept of, so
a tool mapping a schema's owner to a user would find nobody).

It also renders ACL strings the Redshift way. Redshift prefixes a group grantee,
`group analysts=r/bi_analyst`; PostgreSQL unified roles and groups in 8.1 and writes
the identical grant `analysts=r/bi_analyst`. That one is what makes the diff
*converge*: a tool parsing the string would otherwise file the group as a user, read
the group as holding nothing, and re-plan the same `GRANT`s on every pass.

So `redtape export` reads oblako's users, groups, and grants, and `redtape run` plans
`CREATE USER`/`GROUP`, `GRANT`/`REVOKE`, and `ALTER GROUP` against the real Postgres
roles underneath, unchanged. Once the cluster matches the spec the group grants read
back as granted, so the plan has nothing to change.

redtape itself is less steady than that makes it sound. Its diff depends on Python's
per-process hash ordering, so it intermittently re-plans grants that are already in
place, at a rate that varies with the schema. Pinning `PYTHONHASHSEED` makes a plan
reproducible but not correct, because which seeds are clean is a property of the
schema, not of redtape. Treat an empty plan from a single run as weak evidence: apply,
then re-plan. None of this is the compat layer, whose ACL reads are stable.

The catalog compat is installed in every database, not just the one `POSTGRES_DB`
names, because a tool managing a cluster walks `pg_database` and reconnects per
entry.

```{figure} _static/diagrams/access.svg
:alt: redtape spec to redshift-local (svv_*, pg_user/group) to Postgres roles
:width: 100%

Access management as code: a real Redshift access tool reads and applies against
redshift-local, which answers it with the Redshift catalog over real Postgres roles.
```

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

**MPP cluster (opt-in).** The single-node engine is enough for dev/CI, but a
`cluster` profile runs the *same* Redshift-compatible engine on **Citus**, so
tables shard across worker nodes for real multi-node parallelism:

```bash
# name the services so only the cluster starts (a bare `--profile cluster up`
# would also start the single-node `redshift` service and collide on 5439)
docker compose --profile cluster up redshift-coordinator redshift-w1 redshift-w2
```

Everything the single-node image does still works, on the cluster: clients
connect natively (redshift_connector, dbt), the catalog views, date functions,
and plpython UDFs are all present, and a distributed table's aggregations run in
parallel on the workers. **Unmodified Redshift DDL distributes automatically**:
the proxy turns `CREATE TABLE … DISTKEY(col)` into a `create_distributed_table`
(sharded across the workers) and `DISTSTYLE ALL` into a reference table, right
after the CREATE commits; a `SORTKEY` becomes a btree index on those columns.
Tables with no distribution style (EVEN/AUTO) stay local on the coordinator. It
works whether the DDL is autocommitted or inside a transaction (dbt wraps its
models in one): the distribution fires on commit, so rows written in the same
transaction are preserved. So a dbt model with a `dist`/`sort` config, or any
`DISTKEY` DDL, shards with no code change.
The image (`oblako/images/redshift-cluster`) builds from the single-node one and
layers Citus underneath; because Citus can't tolerate a spoofed `server_version`,
the engine reports its real version and the **wire proxy** presents Redshift's
version to clients instead (`OBLAKO_PROXY_SERVER_VERSION`). amd64 only (Citus
ships no arm64 image), so on Apple Silicon it runs under emulation. This is a
distinct product track from the single-node simulator, aimed at self-hosting.

**Redshift ML.** `CREATE MODEL`, `SHOW MODEL [ALL]` and `DROP MODEL [IF EXISTS]`
work from any client, because the wire proxy turns them into calls to functions
in the engine. As on Redshift, `CREATE MODEL` is asynchronous: it validates the
statement and returns, the model is listed in `svv_ml_model_info` as `TRAINING`,
and an agent in the container trains it in a separate container on the host
Docker daemon (through the mounted `/var/run/docker.sock`), then publishes the
prediction function and moves `model_state` to `Model is Ready` (or to the failure
reason).

```sql
CREATE MODEL sandbox.credit
FROM (SELECT f1::float8 AS f_a, f2::float8 AS f_b, target::int AS target
      FROM sandbox.tape WHERE sample = 'train')
TARGET target FUNCTION credit_predict IAM_ROLE default
AUTO OFF MODEL_TYPE xgboost OBJECTIVE 'binary:logistic' PREPROCESSORS 'none'
HYPERPARAMETERS DEFAULT EXCEPT (num_round '150', max_depth '5', eta '0.1')
SETTINGS (S3_BUCKET 'ml-bucket', MAX_RUNTIME 1800);

SELECT model_name, model_state FROM svv_ml_model_info;  -- TRAINING -> Model is Ready
SHOW MODEL sandbox.credit;
SELECT sandbox.credit_predict(f1::float8, f2::float8) FROM sandbox.tape;
```

- `MODEL_TYPE` `XGBOOST`, `MLP` or `LINEAR_LEARNER`, or none for Autopilot, which
  trains all three and keeps the best on a holdout; regression, binary and
  multiclass classification. `FROM` takes a table or a subquery; names may be
  schema-qualified, and the function lands in the model's schema.
- `AUTO OFF` is checked the way Redshift checks it: it needs `MODEL_TYPE
  XGBOOST`, `OBJECTIVE`, `HYPERPARAMETERS` and `PREPROCESSORS`, and at least 500
  training rows. XGBoost hyperparameters (`num_round`, `max_depth`, `eta`,
  `subsample`, `colsample_bytree`, `min_child_weight`, ...) are passed through.
- The prediction function returns the class label for classification (as Redshift
  does for `binary:logistic`). `OBJECTIVE 'reg:logistic'` returns the probability,
  which is what AUC and decile tables need. Autopilot classification models also
  get `<function>_probabilities`, returning `{"probabilities": [...], "labels":
  [...]}` as `SUPER`.
- Features must be numeric (cast them in the `SELECT`); `PREPROCESSORS` other
  than `'none'` are rejected. The model lives in the database it was created in.

**Limitations**

- Redshift physical DDL (`DISTSTYLE`/`DISTKEY`/`SORTKEY`/`ENCODE`) is **accepted
  and ignored** (the bundled wire proxy strips it before the parser), and
  `varchar(max)` is rewritten to `text`, so awswrangler `to_sql`, dbt physical
  configs, and dlt's Redshift destination all work. It has no storage effect on
  the PostgreSQL engine. Late-binding views and `VARBYTE` are still
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

oblako runs SageMaker workloads in **its own Docker containers**, honoring the
real `/opt/ml` contract: input channels are copied in (`put_archive`), the model
artifact is collected from `/opt/ml/model` as `model.tar.gz` (`get_archive`), and
serving containers are polled on `/ping` and invoked on `:8080/invocations`. It
does **not** rely on the SDK's local mode, so it pins `sagemaker>=3.4,<4` (v3) as
a client-only dependency. Three clients all point at the same engine:
`sagemaker` (control plane), `sagemaker-runtime`, and
`sagemaker-featurestore-runtime`.

**The SDK v3 local modes.** Client code can also use the SDK's own local modes:
`ModelTrainer(training_mode=Mode.LOCAL_CONTAINER)`, `ModelBuilder(mode=Mode.LOCAL_CONTAINER)`,
the `LocalSession` endpoint calls, and a local pipeline session. These still reach
for AWS at the edges (IAM role validation, the `sagemaker-<region>-<account>`
default bucket, an ECR pull), so `oblako.engines.sagemaker.use_local_stubs()`
neutralizes those calls. The demo notebooks under `examples/demo-notebooks/` use
this path. Things to know:

- v2 modules (`sagemaker.estimator`, `sagemaker.local`, `sagemaker.workflow`,
  `sagemaker.serializers`, ...) are gone in v3 and raise on import. Training is
  `sagemaker.train.ModelTrainer`, serving `sagemaker.serve.ModelBuilder` or
  `sagemaker.core.local.LocalSession`, pipelines `sagemaker.mlops.workflow`.
- `ModelTrainer`'s `local_container_root` is bind-mounted into the container, so
  keep it under your home directory: Docker Desktop doesn't share the
  `/var/folders` temp dir, and a root there mounts empty. The model lands in
  `<root>/compressed_artifacts/model.tar.gz`.
- A local pipeline session needs both halves v3 splits across two classes:
  `class LocalPipelineSession(sagemaker.mlops.local.local_pipeline_session.LocalPipelineSession,
  sagemaker.core.processing.PipelineSession)`. Register and run it with
  `session.create_pipeline(...)` / `session.start_pipeline_execution(...)`;
  `pipeline.upsert()` / `start()` need a real control plane. Every
  `ProcessingOutput` needs an explicit `s3_uri`.
- `ModelBuilder.deploy_local()` (SDK 3.17) starts the serving container, then
  registers it through `LocalSession.create_endpoint`, which starts a second one on
  the same port and blocks. Call the `LocalSession` endpoint APIs directly instead.

The implemented surface:

- **Training / HPO / processing / transform.** `CreateTrainingJob`,
  `CreateHyperParameterTuningJob` (AMT), `CreateProcessingJob`,
  `CreateTransformJob`, each with its `Describe*`/`List*`/`Stop*` operations,
  running the job's image for real.
- **Endpoints.** `CreateModel` / `CreateEndpointConfig` / `CreateEndpoint` and
  invocation, including **async inference** (`InvokeEndpointAsync`, S3 in and out,
  with an SNS success/error notification) and **multi-model endpoints** (models
  loaded on demand by the `X-Amzn-SageMaker-Target-Model` header).
- **Studio.** `CreateDomain` / `CreateUserProfile` and their lifecycle ops.
- **Feature Store.** `CreateFeatureGroup` + `PutRecord` / `GetRecord` /
  `BatchGetRecord`; the online store is in-memory, the offline store appends
  Parquet to S3.
- **Model Monitor.** Monitoring schedules plus endpoint **data capture** (the
  real `captureData` envelope) written to S3, with a violations report.
- **Model Registry.** Versioned model package groups and packages with approval
  status.
- Tags (`AddTags` / `ListTags` / `DeleteTags`) across all of the above.

**Limitations**

- Requires `pip install 'oblako[sagemaker]'` (v3 SDK, client-only) and Docker.
- No Pipelines API on the engine yet (`CreatePipeline` / `StartPipelineExecution`):
  run pipelines through the SDK's local pipeline session (see above).
- Compute is local Docker containers, not managed instances; `instance_type` is
  cosmetic (`local_gpu` requests all GPUs).
- The Feature Store online store is in-memory (lost on restart); the offline store
  is plain Parquet append, not the managed Glue/Iceberg-cataloged store.
- Model Monitor produces a one-shot violations report, not scheduled drift
  detection. HPO uses a TPE (Syne Tune) or random searcher, not AWS's Bayesian
  tuner. `ServerlessConfig` is accepted and echoed back, but endpoints are still
  container-backed.
