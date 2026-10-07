# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Commits follow `Service(<+|~|->): description`, where `+` = **Added**, `~` =
**Changed**, `-` = **Removed**. Running `cz bump` turns those commits into the
versioned entries below.

## Unreleased

### Added

- **Redshift**: dynamic data masking policies: CREATE, ALTER, DROP, ATTACH and DETACH MASKING POLICY with Redshift Serverless's rules, read back from svv_masking_policy and svv_attached_masking_policy; svv_column_privileges (queries are not masked yet)

## v0.1.0 (2026-10-05)

The first release.

### Added

- **Glue**: triggers and workflows: on-demand, scheduled and conditional triggers, workflow runs that follow the graph
- **Glue**: crawlers and classifiers: S3 targets crawled into tables with partitions and inferred schemas
- **Examples**: the Feast notebook runs on a provisioned cluster or a Redshift Serverless workgroup
- **EventBridge**: Redshift scheduled queries as on AWS: cron schedules, Serverless targets, batches, WithEvent; the proxy is oblako's EventBridge endpoint
- **Redshift**: GetClusterCredentials issues real temporary credentials; every awswrangler.redshift function is tested
- **Glue**: connections: Create, Get, GetConnections, Update, Delete and BatchDelete, so awswrangler's connect(connection=...) works
- **Redshift**: UNLOAD's PARTITION BY, MANIFEST, JSON and compression, with files named as Redshift names them
- **Redshift**: awsdatacatalog.<db>.<table> names, Redshift's auto-mounted Data Catalog; MERGE from a distributed source on Citus
- **Redshift**: ALTER TABLE on Iceberg tables: columns, type widening, compression, partition evolution
- **Redshift**: MERGE into Iceberg tables; Iceberg DDL answers with a command, not a status row
- **Redshift**: Apache Iceberg tables: CREATE EXTERNAL SCHEMA, CREATE TABLE ... USING ICEBERG, INSERT, UPDATE, DELETE
- **Release**: publish oblako to PyPI from a version tag, through trusted publishing
- **CLI**: the oblako banner, in block letters and oblako blue
- **CLI**: oblako configure writes an AWS profile whose endpoints are oblako's
- **MWAA**: environments run AWS's own Airflow image, with DAGs from S3 and InvokeRestApi
- **Redshift**: Serverless namespaces and workgroups, in the API, the Data API and CloudFormation (#52)
- **Redshift**: multi-node clusters from CreateCluster, on a native multi-arch Citus image (#48)
- **RDS**: a real PostgreSQL per DB instance, with read replicas and logical replication (#45)
- **Glue**: the Glue job API, so jobs are created and run with plain boto3 (#42)
- **Athena**: query S3 Tables through s3tablescatalog/<bucket>, as on AWS (#41)
- **S3Tables**: serve the S3 Tables Iceberg REST endpoint, so PyIceberg configured for AWS runs unchanged (#40)
- **Glue**: Parquet and Iceberg tables in the Data Catalog, queried by Athena (#38)
- **S3**: bucket policies and event notifications to Lambda, SQS, SNS and EventBridge (#37)
- **S3**: object and bucket tagging, and S3 Inventory, in front of S3Proxy (#36)
- **CLI**: oblako up starts the in-process API engines, like s3vectors (#35)
- **redshift**: accept PASSWORD DISABLE on CREATE / ALTER USER (#30)
- **redshift**: access management as code via redtape compat (#28)
- **docs**: refresh SDK landing + architecture with diagrams (#24)
- **redshift**: COPY from JSON (auto / noshred / jsonpaths) + compression (#23)
- **services**: local S3 Vectors + S3 Tables (#22)
- **services**: surface ECS + EKS control plane + ECS task metadata endpoint (#20)
- **SageMaker**: multi-model endpoints (MME)
- **EventBridge**: fire Redshift Data targets + scheduled rules
- **Bedrock**: Guardrails (create/get/list/delete + ApplyGuardrail)
- **Athena**: boto3 athena API executed via the Trino engine
- **Firehose**: local Kinesis Data Firehose (DirectPut -> S3)
- **services**: surface moto-backed services (Secrets Manager, SSM, STS, KMS, Logs, ECR, EventBridge)
- **SageMaker**: Model Registry (package groups + versioned packages + approval)
- **Bedrock**: streaming (InvokeModelWithResponseStream + ConverseStream)
- **SageMaker**: async-inference SNS notifications (NotificationConfig)
- **SageMaker**: Asynchronous Inference (InvokeEndpointAsync)
- **DynamoDB**: native vector search (SearchVectors) over DynamoDB Local
- **Bedrock**: real embeddings via invoke_model on embed models
- **Bedrock**: expand foundation-model catalog to what oblako can serve
- **SageMaker**: Model Monitor - Data Capture + monitoring schedules
- **SageMaker**: local Feature Store (online KV + offline S3 Parquet)
- **SageMaker**: serverless-MLflow training example + endpoint variant echo
- **SageMaker**: resource CRUD, tags, Stop*, and Studio domains/profiles
- **SageMaker**: Automatic Model Tuning (HPO) via Syne Tune -> AMT (increment)
- **SageMaker**: local pipeline example + training Environment passthrough
- **SageMaker**: local processing jobs (create_processing_job)
- **SageMaker**: account-free client stubs + local_gpu passthrough
- **SageMaker**: local batch transform (create_transform_job)
- **SageMaker**: local real-time endpoints + invoke_endpoint
- **SageMaker**: local control-plane engine: create_training_job runs in Docker
- **Redshift**: COPY/UNLOAD<->S3 bridge, SUPER/PartiQL, LISTAGG, PIVOT/UNPIVOT (#18)
- **Redshift**: auto-distribute DISTKEY/DISTSTYLE on the cluster (increment #2) (#16)
- **Redshift**: MPP cluster variant on Citus (increment #1) (#15)
- **Redshift**: sqlalchemy-redshift catalog reflection (unblocks Alembic) (#14)
- **Commitizen**: Service(+/~/-) commit format and Keep a Changelog (#12)

### Changed

- **SageMaker**: local pipelines read S3Prefix inputs under when_required; model package versions are never reused
- **Redshift**: each oblako certificate has its own subject, and oblako trust drops stale ones
- **Redshift**: oblako trust survives a redshift-connector reinstall; oblako trust --remove undoes it
- **Examples**: the Feast notebook uses oblako's DynamoDB endpoint, so apply re-runs; outputs refreshed
- **Redshift**: enable_case_sensitive_identifier exists, off as on Redshift, so awswrangler's to_sql(add_new_columns=True) works
- **Redshift**: selected SUPER items are named after the path's last key, cast or not, as Redshift Serverless names them
- **Glue**: a job's S3 output is the data files only, as AWS Glue 5.0 writes it, with no directory markers
- **Redshift**: a SUPER value selected through dot navigation comes back as JSON text, named after its last step, as Redshift sends it
- **CLI**: oblako configure won't drop profile settings a newer oblako wrote, unless --force
- **CloudFormation**: AWS::IAM::Role gets its inline Policies and ManagedPolicyArns, and deletes cleanly
- **S3**: CreateBucket on a bucket you already own succeeds in us-east-1, as on S3; other Regions keep BucketAlreadyOwnedByYou
- **Redshift**: UNLOAD into a non-empty prefix fails unless ALLOWOVERWRITE or CLEANPATH, and files are named after the prefix as written
- **Redshift**: VARCHAR(MAX) is VARCHAR(65535), as Redshift stores and reflects it, so a longer value is refused
- **Redshift**: oblako trust trusts this machine's certificate and any running compose container's, not one or the other
- **Redshift**: Iceberg tables behave as Redshift Serverless does: NOT NULL, plain VARCHAR, Redshift's errors and SHOW TABLE
- **Examples**: every demo notebook re-run against oblako; the fraud stream notebook generates its transactions, so it runs from a fresh clone
- **Services**: KinesisService is exported from oblako.services, like the other services
- **Examples**: the OpenSearch RAG example runs on current opensearch-py, Bedrock batch asks factual prompts; cfn-lint checks the SAM and ECS templates again
- **Examples**: the credit-risk demo notebooks are now credit-scoring
- **Docs**: the architecture diagrams carry the new oblako logo
- **Redshift**: Redshift's storage clauses are stripped only from the CREATE TABLE statements of a multi-statement query
- **Redshift**: docker compose's Redshift containers make their own TLS certificate, and oblako trust reads it while they run
- **Repo**: untrack the docs build, drop local paths from notebook outputs, add SECURITY.md and CONTRIBUTING.md
- **Redshift**: each machine makes its own TLS certificate; the image no longer ships a shared key
- **Docs**: version the README logo URLs, so GitHub's image cache fetches the new logo
- **Services**: containers publish their ports on this machine only
- **CLI**: the dashboard listens on this machine only; OBLAKO_PORT_<NAME> moves a service off a taken port
- **Redshift**: system objects live in pg_catalog, so SQLAlchemy and Alembic see only user tables
- **Docs**: new oblako logo in the README and the docs sidebar
- **ECS**: inject task secrets from SSM and Secrets Manager; refuse invalid Fargate sizes
- **ECS**: plain boto3 RunTask runs the task as a real container
- **CLI**: the oblako profile sends services it does not list to moto, never to AWS
- **CloudFormation**: default port 8017, off OpenSearch Dashboards' 5601
- **CloudFormation**: sam deploy and sam delete run end to end; stacks persist and roll back
- **Redshift**: Serverless tags, and namespaces without an admin user
- **S3**: virtual-hosted addressing, UTF-8 keys, and S3 Control's tag API for buckets
- **MWAA**: report MWAA's defaults and drop null fields, as a real environment does
- **Firehose**: a CreateDeliveryStream that fails on its Kinesis source leaves nothing behind (#51)
- **CLI**: oblako up fails when a service is not ready, with --timeout (#49)
- **Firehose**: name S3 objects, convert to Parquet and COPY into Redshift as AWS does (#47)
- **DynamoDB**: vector search follows the released API, and tables take tags (#46)
- **RDS**: pgvector in the engine, and Data API decimals as strings, as on AWS (#44)
- **Redshift**: AVG of an integer column returns BIGINT, as on Redshift (#43)
- **Kinesis**: start from the CLI and with oblako up, and fix the crash on start (#39)
- **S3**: pin S3Proxy 4.1.1 so paginating partitioned keys terminates (#34)
- **SageMaker**: align with SDK v3, plus in-engine Redshift ML and engine port checks (#32)
- **docs**: redtape's plan is not reliably empty when converged (#31)
- **redshift**: render group ACLs the way Redshift does, so redtape converges (#29)
- **docs**: uniform diagram width so fonts render consistently (#27)
- **docs**: enlarge diagram text for readability on the docs site (#26)
- **redshift**: fix flaky Citus SORTKEY-index test (#25)
- **docs**: reflect new services + license(+): Apache-2.0 open-core (#21)
- **Firehose**: Kinesis-stream source + Redshift destination
- **DynamoDB**: match AWS SearchVectors score semantics (COSINE distance)
- **DynamoDB**: match real AWS SearchVectors wire shape (parity fix)
- **SageMaker**: run training containers directly (v3-ready, no SDK dependency)
- **Redshift**: SORTKEY -> index + transactional-DDL auto-distribute (polish) (#17)
- **Redshift**: bake a fixed TLS cert so pins survive down -v / clones (#13)
