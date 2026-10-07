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
| **S3** | S3 API over the local filesystem (S3Proxy), plus object / bucket tagging, S3 Inventory, bucket policies and event notifications (to Lambda, SQS, SNS, EventBridge). Path-style and virtual-hosted (`bucket.localhost:9000`) addressing, UTF-8 keys, and S3 Control's tag API for buckets (`AWS_ENDPOINT_URL_S3_CONTROL`), which Terraform and Pulumi use. | No flexible-checksum / `aws-chunked`; oblako sets checksum calc `when_required`. Tagging, Inventory, policies and notifications need `oblako up s3` on a Docker-API backend; policies are stored, not enforced. |
| **S3 Tables** | `s3tables` control plane (table buckets, namespaces, tables, `GetTableMetadataLocation`) mapped onto the local **Iceberg REST catalog**; `CreateTable` writes real Iceberg metadata, so the tables are queryable by Athena / Trino / pyiceberg. The **S3 Tables Iceberg REST endpoint** is served at `http://localhost:8013/iceberg`: configure PyIceberg (or Spark) exactly as for AWS, `type=rest`, `warehouse=<table bucket ARN>`, SigV4 on, and change only the `uri`. | Single-warehouse catalog: a table bucket + namespace map to a `[bucket, namespace]` Iceberg namespace prefix; managed maintenance (compaction, snapshot expiry) is not modelled. |
| **S3 Vectors** | `s3vectors`: vector buckets → indexes (dimension + distance metric) → `PutVectors` / `QueryVectors` (k-NN) with Mongo-style metadata filters. Fed by Bedrock embeddings. | Brute-force k-NN (cosine / euclidean), not ANN; vectors are in-memory (not persisted across restart). |
| **DynamoDB** | Amazon's DynamoDB Local behind an oblako proxy on `http://localhost:8007` (`AWS_ENDPOINT_URL_DYNAMODB`), which adds AWS's native **vector search** (boto3 1.43.64+): `VectorIndexes` on `CreateTable` / `VectorIndexUpdates` on `UpdateTable` with `SearchSchema` (a `HASH` vector index partition key, `INLINE_FILTER` attributes), and `SearchVectors` with equality `SearchConditionExpression`, `ProjectionExpression` and the index projection; writes with a vector of the wrong dimensions are rejected. It also adds **tagging** (`TagResource`, `ListTagsOfResource`), which Feast's DynamoDB online store uses. **DynamoDB Streams** are DynamoDB Local's own (`AWS_ENDPOINT_URL_DYNAMODB_STREAMS`, `localhost:8001`). | Single local instance; no Streams→Lambda wiring. `SearchVectors` is exact brute-force k-NN (a full scan), not ANN, and is immediately consistent; `ConsumedCapacity` is not reported. `UpdateItem` isn't checked against vector dimensions. Stream and table ARNs carry DynamoDB Local's region and account (`ddblocal`, `000000000000`). |
| **Kinesis** | Kinesis Data Streams via kinesalite (`saidsef/aws-kinesis-local`). | Streams only; no Managed Flink. (Firehose is a separate service, see Analytics.) |
| **Redshift** | PostgreSQL 16 impersonating Redshift; `redshift-connector`/dbt connect natively. A bundled proxy tolerates physical DDL (`DISTKEY`/`SORTKEY`/`ENCODE`, `varchar(max)`), terminates TLS, and bridges `COPY`/`UNLOAD` to/from `s3://` (Parquet, CSV, and delimited text) so awswrangler, dbt, and Feast load/unload for real. `SUPER` (jsonb-backed) with PartiQL navigation (`data.a.b`, `data['a'][0]`), `LISTAGG` (→ `string_agg`), `PIVOT`/`UNPIVOT` (→ standard SQL), unquoted dateparts (`DATEADD(month, 1, d)`), and native JSON functions are supported. | Row-store, not columnar; late-binding views unsupported; Python UDFs are Python 3; the S3 bridge covers Parquet/CSV/text (not JSON/AVRO/ORC); SUPER dot-navigation yields text (numeric compares need a cast); PIVOT/UNPIVOT need a subquery source (a bare table has no schema in the proxy). |
| **Redshift (control plane)** | `redshift` clusters/nodes/endpoints/snapshots via moto. | Metadata only, the cluster endpoint isn't the queryable engine. |
| **Redshift Data API** | `redshift-data`; SQL executes for real against the engine (through the same proxy, so its `COPY`/`UNLOAD` reach S3 too). Feast's Redshift offline store works end to end. | Statement results buffered in memory. |
| **Redshift ML** | `CREATE MODEL` / `SHOW MODEL` / `DROP MODEL` from any client (psycopg, DBeaver, dbt, the Data API), asynchronous like Redshift: the model trains in a container and `svv_ml_model_info` moves from `TRAINING` to `Model is Ready`; the prediction function runs in-DB. | Needs Docker (the engine mounts `/var/run/docker.sock`); numeric features only (`PREPROCESSORS 'none'`); pure-Python prediction function. |
| **RDS / Aurora** | Real PostgreSQL 16 with **pgvector** (`CREATE EXTENSION vector`), as RDS and Aurora PostgreSQL offer it. The RDS API (`http://localhost:8014`, a proxy over moto) gives each standalone PostgreSQL DB instance its own container at a real endpoint, `<id>.<region>.rds.localhost:<port>`, with `creating` → `available` status, so boto3 waiters work. `CreateDBInstanceReadReplica` starts a **streaming standby** (`pg_basebackup`), read-only until `PromoteReadReplica`; a parameter group with `rds.logical_replication = 1` gives `wal_level = logical` on create or `RebootDBInstance`, so publications and subscriptions work between instances. Aurora clusters and their members share one engine (`localhost:5432`). | Per-instance containers are PostgreSQL only; other engines and Aurora members are control-plane metadata on the shared engine. Instance class, storage and Multi-AZ are not modelled. |
| **RDS Data API** | `rds-data`: synchronous SQL + transactions against the engine. | PostgreSQL semantics; arrays as `arrayValue`, as on AWS. |

## Analytics

| Service | Description | Limitations |
|---|---|---|
| **Athena** | The real boto3 `athena` API (`StartQueryExecution`, `GetQueryExecution`, `GetQueryResults`, `StopQueryExecution`, workgroups) executed via **Trino**, with results written to the S3 `OutputLocation`. `AwsDataCatalog` is the **Glue Data Catalog**, so awswrangler's `read_sql_query` works as is, CTAS included. See "Glue Data Catalog and Athena" below. | Trino SQL dialect, not Athena/Presto-exact; `StopQueryExecution` is best-effort (Trino runs to completion); no federation, `UNLOAD` or Athena's Hive DDL. |
| **Firehose** | `firehose` delivery streams: `DirectPut` and `KinesisStreamAsSource` (read from `LATEST`, as on AWS) sources, buffered and flushed on the interval or size to **S3** or **Redshift**. S3 objects are named as on AWS (`<prefix><stream>-1-yyyy-MM-dd-HH-mm-ss-<uuid>`, `yyyy/MM/dd/HH/` appended to a prefix without a `!{timestamp:...}` expression; `!{timestamp:...}` and `!{firehose:random-string}` evaluated; `.gz` for GZIP). **Record format conversion** writes Parquet with the column types of a Glue table (`pip install 'oblako[parquet]'`); records that don't convert go to the `ErrorOutputPrefix` as `format-conversion-failed`. The **Redshift** destination stages each batch to S3 and runs Firehose's `COPY ... FROM 's3://...' CREDENTIALS ... <CopyOptions>` on the cluster, so `CopyOptions` such as `JSON 'auto' GZIP` apply as on AWS. Stream definitions persist across restarts. | Destinations: S3 and Redshift only. No Lambda transformation, dynamic partitioning, ORC, or ZIP/Snappy compression; no `UpdateDestination` (the version is always 1). Buffering floors aren't enforced, and records still buffered when the engine stops are lost. |
| **Glue (jobs)** | The boto3 `glue` job API (`create_job`, `start_job_run`, `get_job_run`, `get_job_runs`, ...) on the Glue engine (:8486): a run fetches `Command.ScriptLocation` from S3 and runs it in the official `amazon/aws-glue-libs:5` image (per-job container), with Glue's arguments (`--JOB_NAME`, the job's arguments) so `getResolvedOptions` works. Spark's `s3://` and `s3a://` reach oblako's S3, and the container's AWS SDKs reach oblako's Glue and S3 (`AWS_ENDPOINT_URL_GLUE` / `_S3`), so `from_catalog` reads Data Catalog tables and scripts carry no endpoint. Output goes to CloudWatch Logs `/aws-glue/jobs/output` and `/error` (in moto). | ~5 GB image; workers, worker types and job bookmarks aren't modelled (one local Spark); sequential workflows only (no full DAGs/crawlers). |
| **Glue Data Catalog** | boto3 `glue` databases, tables, partitions, column statistics and connections; **crawlers** (S3 targets: one table per dataset folder, Hive or `partition_N` partitions, schemas from Parquet footers, CSV headers or JSON lines, `TablePrefix`, schema-change policy, cron `Schedule`) and **classifiers** (CSV and JSON applied; Grok and XML stored); **triggers** (on-demand, cron-scheduled, conditional on job and crawl states) and **workflows** (`StartWorkflowRun` follows the graph, jobs get `--WORKFLOW_NAME`/`--WORKFLOW_RUN_ID`, run properties, `IncludeGraph`): Parquet / CSV / JSON tables (awswrangler, Athena CTAS) and Iceberg tables (PyIceberg's Glue catalog), the latter in the same **Iceberg REST catalog** as S3 Tables. Trino's metastore for Athena. | No crawlers, connections or Lake Formation; Glue can't write Iceberg metadata itself (`OpenTableFormatInput`). |

## Orchestration & compute

| Service | Description | Limitations |
|---|---|---|
| **MWAA (Airflow)** | `mwaa` on `http://localhost:8016`: each environment runs AWS's own MWAA Airflow image (PostgreSQL, ElasticMQ, webserver, scheduler, worker); DAGs sync from the S3 source bucket; `InvokeRestApi` reaches Airflow's REST API; tasks reach oblako's services. | The image builds on first use (about 6 minutes). Auth is Airflow's simple auth manager (every user an admin); `CreateCliToken`, `CreateWebLoginToken` and CloudWatch logging are not simulated; capacity and network settings are recorded only. |
| **Step Functions** | Amazon's `aws-stepfunctions-local`. | Lambda-backed states need a running SAM CLI. |
| **Lambda** | Control plane + **real Docker-based invocation**. | x86_64 + python3.12 runtime image; SAM CLI for the dev-loop. |
| **ECS / Fargate** | `ecs` control plane (moto) + **each task is a real container**: `oblako up ecs` serves the ECS API on :8018 (`AWS_ENDPOINT_URL_ECS`), where plain boto3 `run_task` launches the image, `describe_tasks` reports each container's state and exit code, and the `tasks_stopped` waiter works. Container `secrets` are resolved from SSM Parameter Store or Secrets Manager (including `:json-key`) when the task starts, and Fargate task sizes AWS does not offer are refused at registration with AWS's `ClientException`. A secret that cannot be read fails `run_task` itself, where AWS starts the task and stops it with `ResourceInitializationError`; `create_service` and CloudFormation services launch containers too. Containers are wired to oblako's endpoints and published on a host port. Tasks also get the **ECS task metadata endpoint** (`ECS_CONTAINER_METADATA_URI[_V4]`) that AWS containers read. | Fargate launch type; no continuous reconciliation/autoscaling; per-task compute needs a Docker socket. |
| **EKS** | `eks` control plane (moto): create/describe/list clusters. | Control-plane metadata only; no real Kubernetes data plane (for local k8s, use the Kubernetes container backend). |
| **ELBv2 (ALB)** | `elbv2` control plane (moto) + **each load balancer is a real Caddy reverse proxy** that round-robins to the targets with the target group's health check; `DNSName` resolves to `localhost:<port>`. | ALB (HTTP); no listener-rule path routing yet; NLB/GWLB not modelled. |
| **API Gateway** | External, AWS SAM CLI (`sam local start-api`) routing HTTP to your functions. | oblako doesn't manage it; bring your own SAM CLI. |

## Management & control planes

| Service | Description | Limitations |
|---|---|---|
| **CloudFormation** | `cloudformation` (+ `aws cloudformation deploy` / `sam deploy`, or `CreateStack`) provisions **real** oblako resources, including a full ECS Fargate + ALB stack and Redshift Serverless. Stacks persist across restarts; a failed resource rolls the stack back (`ROLLBACK_COMPLETE`) as on AWS; `GetTemplate`, `GetTemplateSummary` and `CreateStack` are supported, so `sam deploy` / `sam delete` run end to end. | Subset of resource types (S3, DynamoDB, Redshift, Redshift Serverless, RDS, ECS, ELBv2, …). |
| **IAM / STS** | moto control plane + oblako's policy evaluator. | Policy evaluation is a best-effort reimplementation. |
| **EC2** | moto control plane + real container-backed instances. | `describe_*` fidelity; instances are containers, not VMs. |
| **OpenSearch** | OpenSearch single-node (Knowledge Bases / RAG). | Security plugin disabled for local use. |
| **AppConfig** | Python reimplementation (control + data plane + rule evaluation). | Reimplementation, not the AWS engine. |
| **EventBridge** | `events` control plane (moto) behind oblako's EventBridge endpoint (:8010), a proxy that **fires scheduled rules**, `rate(...)` and `cron(...)` (UTC, with `?`, `L` and `#`), and runs **Redshift Data** targets: Redshift's scheduled queries (query editor v2's `QS2-` rules). A target on a cluster or a Serverless workgroup runs `Sql` as `ExecuteStatement` or `Sqls` as one `BatchExecuteStatement`, with `DbUser` or `SecretManagerArn`; `WithEvent` puts the "Redshift Data Statement Status Change" event on the bus for other rules to route. | moto delivers SQS/SNS/Lambda targets of `PutEvents` itself; the proxy adds scheduling, Redshift Data targets and AWS service events. Starts with `moto`. |
| **Common services (moto)** | Surfaced as-is via the moto container so unmodified boto3 works: `sns`, `sqs`, `sts`, `secretsmanager`, `ssm`, `kms`, `cloudwatch` (metrics), `logs` (CloudWatch Logs), `ecr`. | moto's control-plane fidelity; no data-plane behavior beyond what moto implements. |

---

The deep dives below cover the services with the most local-specific behavior.

## Infrastructure as code

CloudFormation and SAM run on oblako's CloudFormation engine. Terraform and Pulumi
call each service's API directly, and reach oblako through the profile that
`oblako configure` writes: with `AWS_PROFILE=oblako`, the AWS provider of both
takes every endpoint from the profile's `services` section, with no
provider-specific settings. Checked with Terraform 1.16.4 and its AWS provider
6.67.0, Pulumi 3.267.0 with `pulumi-aws` 7.48.0, and SAM CLI 1.166.2: each
created, re-planned without changes, and destroyed an S3 bucket, an IAM role and a
Redshift Serverless namespace and workgroup. The Go SDK they use addresses
buckets as `bucket.localhost:9000` and reads bucket tags through S3 Control, which
is why oblako serves both.

## MWAA (Airflow)

`oblako up mwaa` starts the MWAA API on port 8016
(`AWS_ENDPOINT_URL_MWAA=http://localhost:8016`). `CreateEnvironment` runs the
containers Amazon MWAA runs, from the images AWS publishes as source in
[aws/amazon-mwaa-docker-images](https://github.com/aws/amazon-mwaa-docker-images)
(Apache-2.0): PostgreSQL for Airflow's metadata, ElasticMQ as the Celery queue,
then the webserver, scheduler and worker, on a network of their own. The image is
built from a pinned commit of that repository the first time a version is used
(about 6 minutes and 4.7 GB for 3.3.1); later environments start in under a
minute.

```python
mwaa = boto3.client("mwaa")
mwaa.create_environment(
    Name="etl", AirflowVersion="3.3.1",
    SourceBucketArn="arn:aws:s3:::my-dags", DagS3Path="dags",
    ExecutionRoleArn="arn:aws:iam::123456789012:role/mwaa",
    NetworkConfiguration={"SubnetIds": ["subnet-1", "subnet-2"]},
)
# CREATING, then AVAILABLE; WebserverUrl is localhost:<port>
mwaa.invoke_rest_api(Name="etl", Path="/dags", Method="GET")
```

DAG files under `DagS3Path` are mirrored into the environment every 10 seconds,
as MWAA syncs them. `requirements.txt`, the plugins zip and the startup script are
read when the environment is created or updated (`UpdateEnvironment` restarts
Airflow with them). Tasks use oblako's services through the same
`AWS_ENDPOINT_URL_*` settings as `oblako notebook`, so a DAG's plain boto3 calls
reach oblako. Airflow 2.9.2 to 3.3.1 are accepted; 3.3.1 is the version tested.

boto3 prefixes MWAA's hostnames, so a client pointed at `localhost:8016` calls
`api.localhost:8016` and `env.localhost:8016`. macOS and Linux hosts with
systemd-resolved resolve those to the loopback address (the engine listens on
IPv4 and IPv6); inside a plain container they don't resolve, so code there needs
`Config(inject_host_prefix=False)`.

`GetEnvironment` reports the settings MWAA reports for an environment created
without them (one worker, scheduler and webserver for `mw1.micro`; task logs on,
the others off; `EndpointManagement: SERVICE`), and `InvokeRestApi` drops null
fields from Airflow's responses, as MWAA does. Both were checked against a real
MWAA environment (Airflow 3.3.1, `mw1.micro`), which ran the same DAG, returned
the same `RestApiClientException` codes and messages, and took a similar few
minutes to list a new DAG file.

The webserver uses Airflow's simple auth manager with every user an admin (AWS's
`testing` auth type), so it accepts any login. `CreateCliToken`,
`CreateWebLoginToken` and CloudWatch logging are not simulated; environment class,
worker counts and network settings are recorded and reported, not enforced.

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
`months_between`, `trunc(timestamp)`, `convert_timezone`. `AVG` of a SMALLINT,
INTEGER or BIGINT column returns BIGINT, truncated, as Redshift's does (PostgreSQL
returns NUMERIC): the proxy routes `avg(` to aggregates in schema `pg_oblako`,
which copy PostgreSQL's `avg` for every other type. The date parts must be
quoted (`dateadd('day', 7, ts)`), as most SQL generators emit them. It also adds
the Redshift **catalog views** BI tools and dbt query for metadata, mapped onto
PostgreSQL's catalogs: `pg_table_def`, `svv_tables`, `svv_columns`,
`svv_table_info`, and (empty) `svv_external_schemas` / `svv_external_tables` /
`svv_external_columns`.

**SQLAlchemy / Alembic.** The `sqlalchemy-redshift` dialect
(`redshift+redshift_connector://…`) reflects too: its introspection reads
Redshift-only catalog columns (`reldiststyle`, `attencodingtype`, …) and filters
by output-column aliases in `WHERE`, neither of which stock PostgreSQL has, so the
proxy answers them. `get_columns`, table autoload, `has_table`, ORM models with
`redshift_diststyle`/`redshift_distkey`/`redshift_sortkey`, and **Alembic
autogenerate** work against the engine (the driver is a client dependency, nothing
is added to the image). As on Redshift, the system tables, `svv_*` views and
built-in functions live in `pg_catalog` and oblako's internals in the `pg_oblako`
schema, so `public` holds only your objects: reflection lists no `stl_*`/`svv_*`
relations, and autogenerate, with or without `include_schemas`, proposes no change
to them. Data volumes from older images are migrated on start.

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

**Users, groups and roles.** Redshift keeps three kinds of identity apart, and so
does redshift-local: `CREATE USER`, `CREATE GROUP` with `ALTER GROUP ... ADD USER`,
and Redshift's role-based access control: `CREATE ROLE`, `GRANT ROLE r TO user`,
`GRANT ROLE r TO ROLE r2`, `REVOKE ROLE`, and `TO ROLE r` as the grantee of a
privilege or a default privilege. Underneath they are all PostgreSQL roles; a
Redshift role is one that can't log in and is marked as a role, so `pg_group` lists
only real groups, as on Redshift, and an ACL string prefixes only a group.

The privilege views access tools read instead of ACL strings answer with
Redshift's columns and values (`identity_type` of `user`, `group`, `role` or
`public`; explicit grants only, not what an owner holds on its own object):
`svv_roles`, `svv_user_grants`, `svv_role_grants`, `svv_relation_privileges`,
`svv_schema_privileges`, `svv_database_privileges`, `svv_function_privileges` and
`svv_default_privileges`. The same scenario run on Redshift Serverless (2026-10-06)
gives the same rows in every one of them. As on Redshift, an ACL string leaves out
grants to roles, which show only in these views.

What still differs from Redshift Serverless here: an ACL string spells an owner's
privileges PostgreSQL's way (`arwdDxt`), where Redshift writes its own letters
(`arwdRxtDPA`); Serverless has built-in `sys:*` roles (`sys:dba`, `sys:superuser`,
...) that redshift-local doesn't; and on Redshift the user an IAM identity maps to
is created at its first login, without a password, so making it a superuser needs
a password in the same statement (`ALTER USER "IAM:x" PASSWORD '...' CREATEUSER`),
whereas redshift-local creates it when the credentials are issued.

**Dynamic data masking.** `CREATE MASKING POLICY [IF NOT EXISTS] p WITH (inputs)
USING (expression)`, `ALTER MASKING POLICY p USING (...)`, `DROP MASKING POLICY p`,
`ATTACH MASKING POLICY p ON t (cols) [USING (inputs)] TO { user | ROLE r | PUBLIC }
[PRIORITY n]` and `DETACH MASKING POLICY ... FROM ...` keep policies and their
attachments, read back from `svv_masking_policy` and `svv_attached_masking_policy`
with Redshift's columns and JSON formats. The rules are the ones Redshift
Serverless enforces (checked 2026-10-07): a different policy can't share a
priority on a column, one policy can go to several grantees at one priority and to
one grantee at several, one `DETACH` removes all of a grantee's, `DROP` is refused
while the policy is attached, `ALTER` keeps the output type exactly
(`varchar(64)` and `varchar(10)` clash), an expression of ambiguous type (a bare
`'***'`) is refused until cast (`'***'::varchar(256)`), `TO GROUP` is a syntax
error, dropping a table drops its attachments, and only a superuser manages or sees
policies. `svv_column_privileges` lists column-level grants.

A query reads each masked column as the user's highest-priority attachment gives
it: the user's own, a role it has (directly or through other roles), or PUBLIC.
The proxy replaces a masked table read in a query (in a FROM list, after JOIN, in
subqueries and CTEs, not the target of INSERT, UPDATE or DELETE) with a SELECT
that applies the policies, named as the table, and the policy is chosen per
query for the current user. The column keeps its type, and the table stays a
plain table: writes, ALTER TABLE and DROP TABLE work as before. ALTER, ATTACH and
DETACH take effect for the next query.

As on Redshift Serverless (checked 2026-10-07): a filter on a masked column
(`WHERE email = ...`, `LIKE`, `count(DISTINCT ...)`) sees the masked value; a
superuser is masked like anyone else, by its own grants (it is not taken to hold
every role); and a user granted only some columns of a masked table reads those,
`*` included, while a column it wasn't granted is refused.

What differs: naming a column the user wasn't granted fails with "column does
not exist" here, where Redshift says "permission denied for relation"; and
Redshift stores an expression in its own normalised form (`'***'::varchar(256)`
becomes `CAST(CAST('***' AS VARCHAR) AS VARCHAR(256))`), where redshift-local keeps
it as written, so tools should compare expressions by round trip rather than by
text; and some output types are named differently (Redshift has no `text`, so a
`::text` cast reads as `character varying` there).

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

**TLS.** The bundled proxy terminates SSL with a self-signed certificate for
`localhost` and `127.0.0.1`. oblako makes it once per machine, in
`~/.oblako/redshift/tls`, and mounts it into every Redshift container, the
single-node engine and each node of a multi-node cluster. The key never leaves
your machine, and the certificate cannot sign others (`CA:FALSE`). It survives
container recreates and volume resets, so a pinned `sslrootcert` or `oblako trust`
stays valid. To use your own, replace the two files there. A container started
by plain `docker compose` makes its own certificate; `oblako trust` trusts it too
when it is running, besides the machine's.

- **libpq clients** (psycopg, and JDBC tools like Metabase) work out of the box
  with `sslmode=require` (encrypt), or `verify-full` with `sslrootcert` pointed at
  `~/.oblako/redshift/tls/server.crt`.
- **redshift_connector (dbt, awswrangler)** verifies only against a hardcoded
  Amazon CA bundle with no override, so it can't verify a local cert by default.
  Run **`oblako trust`** once: it appends this machine's certificate to that venv's
  redshift-connector bundle, then use `sslmode: verify-ca` for verified TLS (no
  `ssl=False`). It also installs a small keeper in the venv (a `.pth` file and the
  module it imports) that puts the certificate back at interpreter start, so the
  trust survives a `redshift-connector` reinstall, which restores the pristine
  bundle. Run it once per venv or CI runner; `oblako trust --remove` undoes it.
  Without trust, use `sslmode: disable` locally.
- **Before oblako 0.1.0** the image carried one shared certificate whose key was
  public. `oblako trust` removes it from any bundle it had been added to.

`OBLAKO_SSL=0` turns TLS off entirely.

**Multi-node clusters.** The Redshift API on `http://localhost:8015` (a proxy over
moto, started by `oblako up redshift`) makes clusters real. A single-node cluster's
endpoint is the shared engine, `localhost:5439`. A multi-node cluster runs as its
own Citus cluster, the same Redshift-compatible engine with Citus underneath:

```python
redshift.create_cluster(ClusterIdentifier="analytics", ClusterType="multi-node",
                        NodeType="ra3.large", NumberOfNodes=2,
                        MasterUsername="admin", MasterUserPassword="...", DBName="dev")
redshift.get_waiter("cluster_available").wait(ClusterIdentifier="analytics")
```

The cluster is a leader node (the Citus coordinator, which holds no data) and
`NumberOfNodes` compute nodes (Citus workers), at
`<id>.<region>.redshift.localhost` on a port of its own; `DescribeClusters`
reports `creating` until every node is registered, and `ClusterNodes` lists each
node's role and address. **Unmodified Redshift DDL distributes**: the proxy turns
`CREATE TABLE … DISTKEY(col)` into `create_distributed_table` and `DISTSTYLE ALL`
into a reference table before the `CREATE` returns; a `SORTKEY` becomes a btree
index. Tables with no distribution style stay on the leader. The Data API reaches
a cluster by `ClusterIdentifier`. Up to 8 compute nodes per cluster.

**Redshift Serverless.** The same port answers the `redshift-serverless` API
(`AWS_ENDPOINT_URL_REDSHIFT_SERVERLESS=http://localhost:8015`). A namespace
creates its admin user, with the given password, and its database (`dev` by
default) in the engine; every workgroup's endpoint is the shared engine,
`localhost:5439`, whatever `port` is requested:

```python
rss = boto3.client("redshift-serverless")
rss.create_namespace(namespaceName="analytics", adminUsername="admin",
                     adminUserPassword="...", dbName="dev")
rss.create_workgroup(workgroupName="analytics", namespaceName="analytics")
rss.get_workgroup(workgroupName="analytics")["workgroup"]["endpoint"]
# {'address': 'localhost', 'port': 5439}
```

`DeleteNamespace` drops the user and database it created, and refuses while a
workgroup uses the namespace (`ConflictException`). The Data API takes
`WorkgroupName` in place of `ClusterIdentifier`, and CloudFormation creates
`AWS::RedshiftServerless::Namespace` and `::Workgroup` with their `GetAtt`
attributes (`Workgroup.Endpoint.Address`, ...), and `TagResource`, `UntagResource` and
`ListTagsForResource` work on namespaces and workgroups. Base and maximum capacity, VPC
settings, snapshots, usage limits and `GetCredentials` are not simulated: a
workgroup has the engine's resources, and the API accepts and reports the
capacity and network settings it is given.

The image (`oblako/images/redshift-cluster`) builds from the single-node one and
compiles Citus from source, so it is native on amd64 and arm64. Because Citus
can't tolerate a spoofed `server_version`, the engine reports its real version and
the **wire proxy** presents Redshift's version to clients instead
(`OBLAKO_PROXY_SERVER_VERSION`). For self-hosting without oblako's API, the compose
`cluster` profile runs the same image:

```bash
docker compose --profile cluster up redshift-coordinator redshift-w1 redshift-w2
```

**Passwords.** The engine checks passwords as Redshift does when
`POSTGRES_HOST_AUTH_METHOD` is `md5` (oblako's default); a deployment that sets
`trust` keeps passwordless logins.

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

**COPY, UNLOAD and awswrangler.** `COPY` loads Parquet, CSV, JSON and delimited
text from S3, and `UNLOAD` writes them, as Redshift does: files named after the
prefix as written (`venue_0000_part_00`, `000` with `PARALLEL OFF`; `.parquet` for
Parquet, `.gz`/`.bz2`/`.zst` when compressed, or `EXTENSION`), `PARTITION BY (...)
[INCLUDE]` into Hive-style folders, `MANIFEST [VERBOSE]`, and a refusal to write
into a non-empty prefix unless `ALLOWOVERWRITE` or `CLEANPATH`. Every function in
awswrangler's `wr.redshift` works against redshift-local: `to_sql` in each mode
and with Redshift's table options, `copy`, `unload` and `unload_to_files`, and all
three ways to connect: a Secrets Manager secret, a Glue connection, and
`connect_temp`, whose `GetClusterCredentials` call issues an `IAMA:<user>` login on
the cluster's engine that acts as `<user>` until it expires.

`GetClusterCredentialsWithIAM` (a provisioned cluster) and Redshift Serverless's
`GetCredentials` (a workgroup) work too: the database user is the calling IAM
identity, `IAM:<user>` or `IAMR:<role>` for an assumed role, created on first use
with a password that expires, and refreshed on every call. Like any new Redshift
user it holds no privileges until granted them (`ALTER USER "IAM:ops" CREATEUSER`
makes it a superuser, `NOCREATEUSER` takes that back).

**SUPER.** `SUPER` columns take JSON (`json_parse`, nested Parquet through
`COPY`) and PartiQL navigation, with dots or brackets: `data.customer.name`,
`data.items[0].sku`, `data['customer']['name']`. A navigated value selected on its
own comes back as Redshift sends SUPER to a driver, as JSON text: `"Ann"`, with its
quotes, in a column named after the last step (`name`). Cast it for the plain value
(`data.customer.name::varchar`). Inside an expression or a filter, a navigated value
is text, so `WHERE data.type = 'premium'` works; compare numbers through a cast
(`data.age::int > 30`). A whole `SUPER` column, or a bracket path, comes back as
PostgreSQL `jsonb`, which some drivers decode into Python values.

**Apache Iceberg tables.** Redshift creates and writes Iceberg tables registered
in the Glue Data Catalog, and so does redshift-local. oblako's Glue catalog keeps
Iceberg tables in its Iceberg REST catalog on S3Proxy, so a table Redshift writes
is the table Athena, Trino, Spark and PyIceberg read, and a table they create
shows up in Redshift. Start the catalog with `oblako up iceberg` (and `s3`).

```sql
CREATE EXTERNAL SCHEMA lake FROM DATA CATALOG DATABASE 'sales'
IAM_ROLE default CREATE EXTERNAL DATABASE IF NOT EXISTS;

CREATE TABLE lake.orders (order_id int, order_date date, total decimal(10,2))
USING ICEBERG LOCATION 's3://my-lake/orders/'
PARTITIONED BY (month(order_date))
TABLE PROPERTIES ('compression_type'='snappy');

INSERT INTO lake.orders VALUES (1, '2024-10-30', 299.99);
UPDATE lake.orders SET total = 310 WHERE order_id = 1;
DELETE FROM lake.orders WHERE order_date < '2024-01-01';
SHOW TABLE lake.orders;
DROP TABLE lake.orders;  -- removes the catalog entry; the files stay
```

- `CREATE TABLE ... USING ICEBERG` takes `LOCATION` (required, and empty, as on
  Redshift), `PARTITIONED BY` with `identity`, `bucket(N, col)`,
  `truncate(W, col)`, `year`, `month`, `day` and `hour`, and `TABLE PROPERTIES`
  `compression_type` (`zstd` by default). `CREATE TABLE ... AS SELECT` works too.
- As on Redshift, strings are `VARCHAR` without a length (`VARCHAR(N)` is
  refused), and `NOT NULL` is the one column attribute taken; other constraints
  and attributes are refused. Errors carry Redshift's own messages, checked
  against Redshift Serverless.
- `SELECT` scans the table, so joins with local tables are plain SQL. `INSERT`,
  `UPDATE` and `DELETE` work from any client, parameterized statements included;
  `MERGE` works too, written without bind parameters. An insert appends; any other
  write rewrites the table's data files.
- Each write commits as one Iceberg snapshot when its transaction commits, and
  `ROLLBACK` writes nothing. As on Redshift, a transaction takes one Iceberg write.
- `ALTER TABLE` renames, adds and drops columns, widens a column's type (`int` to
  `bigint`, `real` to `double precision`, a decimal's precision), sets
  `TABLE PROPERTIES ('compression_type'=...)`, and evolves the partition spec with
  `ADD`, `DROP` and `REPLACE PARTITION FIELD`. These change metadata only.
- Redshift's auto-mounted catalog works without `CREATE EXTERNAL SCHEMA`:
  `SELECT * FROM awsdatacatalog.sales.orders` reaches Glue database `sales`, which
  oblako mounts as the schema `"awsdatacatalog.sales"` on first use.
- DDL completes as a command, with no result rows, as on Redshift.
- `svv_external_schemas`, `svv_external_tables` and `svv_external_columns` list
  the external schemas and their tables.
- Not yet: Iceberg v3 tables (`'format-version'='3'`, which Redshift Serverless
  also refused when this was checked) and column `DEFAULT` values, S3 table
  buckets (`"<bucket>@s3tablescatalog"` names), and nested Iceberg types in
  writes. A table another engine creates after `CREATE EXTERNAL SCHEMA` appears
  once you run `CREATE EXTERNAL SCHEMA IF NOT EXISTS` again (an `awsdatacatalog`
  name picks it up by itself).

**Limitations**

- Redshift physical DDL (`DISTSTYLE`/`DISTKEY`/`SORTKEY`/`ENCODE`) is **accepted
  and ignored** (the bundled wire proxy strips it before the parser), and
  `varchar(max)` becomes `varchar(65535)`, as Redshift stores it, so awswrangler
  `to_sql`, dbt physical configs, and dlt's Redshift destination all work. It has no storage effect on
  the PostgreSQL engine. Late-binding views and `VARBYTE` are still
  unsupported.
- It's a row-store PostgreSQL, not columnar: no Redshift-style column compression
  / zone maps / sort-key ordering. Query **results** are faithful; storage and
  performance characteristics are not.
- Python UDFs run as **Python 3** (real Redshift's are Python 2, which Amazon is
  sunsetting); `LANGUAGE plpythonu` is aliased to the Python 3 handler.
- It's a PostgreSQL engine underneath, no columnar storage, distribution, or
  the Redshift query planner.

## Glue Data Catalog and Athena

The Glue engine (`oblako up glue`, :8486) is the catalog Athena, Trino and the
`glue` client share. It keeps two kinds of table:

- **Parquet, CSV and JSON tables** under an S3 location, with their partitions
  and column statistics, in SQLite (`~/.oblako/glue/catalog.db`). This is what
  `wr.s3.to_parquet(..., database=, table=)` creates, and what Athena CTAS
  writes.
- **Iceberg tables**, kept in the Iceberg REST catalog that S3 Tables, Spark and
  Trino's `iceberg` catalog use. PyIceberg's Glue catalog
  (`load_catalog(type="glue", **{"glue.endpoint": ...})`) works unchanged:
  `CreateTable` registers its metadata file, and each commit's `UpdateTable`
  moves the table to the new file, refused if another writer got there first.
  Tables created through the REST catalog show up in Glue too.

Each database is also a REST namespace. Trino's `awsdatacatalog` catalog is the
Hive connector with this engine as its Glue metastore, and Iceberg tables in it
are redirected to the `iceberg` catalog. Athena's `AwsDataCatalog` maps to it,
so a query sees both kinds of table. Start them with `oblako up iceberg` and
`oblako up trino` (which starts the Glue engine too).

Athena:

- **Workgroups.** `get` / `list` / `create` / `update` / `delete_work_group`,
  kept in `~/.oblako/athena/workgroups.json`. Queries without an
  `OutputLocation` use the workgroup's, and `EnforceWorkGroupConfiguration`
  overrides theirs, as on AWS. Locally `primary` comes configured with
  `s3://oblako-athena-results/`, created on first use.
- **CTAS.** `CREATE TABLE ... WITH (...) AS SELECT` creates a Glue table. Its
  data goes under `external_location`, or `<output>/tables/<query id>/` without
  one. Athena's properties are translated for Trino (`write_compression`,
  `field_delimiter`), and with `table_type = 'ICEBERG'` the table is created
  in the `iceberg` catalog. The query's `DataManifestLocation` lists the files
  written, which is how awswrangler's default `ctas_approach=True` reads results.

**Limitations**

- `GetPartitions` filters support Glue's documented subset (`=`, `<>`, `<`,
  `>`, `BETWEEN`, `IN`, `LIKE`, `IS NULL`, `AND` / `OR` / `NOT`). Anything else
  is refused with `InvalidInputException`.
- No crawlers, connections, user-defined functions, table versions or Lake
  Formation. Glue can't create Iceberg metadata itself (`OpenTableFormatInput`),
  so create Iceberg tables with PyIceberg, Spark or Trino.
- Athena runs Trino's SQL dialect: Athena's Hive DDL (`CREATE EXTERNAL TABLE ...
  ROW FORMAT`, `MSCK REPAIR TABLE`) and `UNLOAD` aren't translated. Query
  results report no scanned bytes.

## S3

S3 API backed by S3Proxy over the local filesystem. S3Proxy doesn't implement
tagging, Inventory, bucket policies or event notifications, so `oblako up s3`
puts a stock nginx on :9000 in front of it: every request goes straight to
S3Proxy (on :9001), except those calls, which an oblako engine answers. nginx
also logs each completed write to a file the engine follows, for notifications.

- **Tagging.** `put_object(..., Tagging="k=v")`, `put_object_tagging` /
  `get_object_tagging` / `delete_object_tagging`, tags on multipart uploads and
  copies (`TaggingDirective` `COPY` or `REPLACE`), and bucket tagging. Tags
  belong to an object version, as on S3: overwrite the object and its tags go.
  S3's limits apply (10 tags per object, key 128 / value 256 characters).
- **Inventory.** `put` / `get` / `list` / `delete_bucket_inventory_configuration`.
  S3 takes up to 48 hours for the first report; oblako writes it when the
  configuration is saved, then daily (`OBLAKO_S3_INVENTORY_INTERVAL` seconds):
  a CSV (gzipped) or Parquet data file plus `manifest.json` under
  `<prefix>/<source>/<id>/` in the destination bucket.
- **Bucket policy.** `put` / `get` / `delete_bucket_policy` and
  `get_bucket_policy_status` (public when anyone is allowed unconditionally).
  Stored and returned only: S3Proxy has no IAM, so nothing is enforced.
- **Event notifications.** `put` / `get_bucket_notification_configuration`
  with Lambda, SQS and SNS destinations (in moto, :5500) and EventBridge, with
  prefix / suffix filters. `ObjectCreated` (Put, Copy, CompleteMultipartUpload)
  and `ObjectRemoved:Delete` events are delivered as S3 event records once the
  write lands, so an upload to `raw/` can invoke a Lambda that writes to
  `curated/`.
- **Headers S3Proxy rejects.** boto3 sends `x-amz-security-token` with
  session credentials (inside Lambda, with SSO or an assumed role), and newer
  AWS SDKs ask listings for `x-amz-optional-object-attributes` (Trino, Spark).
  S3Proxy answers either with `NotImplemented`, so the front drops both.

**Limitations**

- S3Proxy doesn't implement the AWS SDK v2 default flexible checksums
  (`x-amz-checksum-*` over `aws-chunked`); oblako sets checksum calculation to
  `when_required` so uploads work.
- Tags are stored and returned, but nothing acts on them: S3Proxy has no
  lifecycle rules or tag-based access control. Bucket policies likewise aren't
  enforced. Batch deletes (`DeleteObjects`) don't produce events: their keys
  are in the request body, which isn't logged. Inventory doesn't produce ORC,
  and Parquet reports need `pyarrow`.
- These additions need the front, which `oblako up s3` starts on a
  Docker-API backend (Docker, Podman, Colima). On Kubernetes or Apple
  `container`, plain `docker compose up`, or with `OBLAKO_S3_EXTENSIONS=0`,
  S3Proxy serves :9000 directly and those calls return `NotImplemented`.
- Large downloads through the front run at several hundred MB/s rather than
  S3Proxy's direct speed; small requests show no measurable difference.

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
