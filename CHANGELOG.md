# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Commits follow `Service(<+|~|->): description`, where `+` = **Added**, `~` =
**Changed**, `-` = **Removed**. Running `cz bump` turns those commits into the
versioned entries below.

## Unreleased

### Added

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
- **SageMaker**: local control-plane engine — create_training_job runs in Docker
- **Redshift**: COPY/UNLOAD<->S3 bridge, SUPER/PartiQL, LISTAGG, PIVOT/UNPIVOT (#18)
- **Redshift**: auto-distribute DISTKEY/DISTSTYLE on the cluster (increment #2) (#16)
- **Redshift**: MPP cluster variant on Citus (increment #1) (#15)
- **Redshift**: sqlalchemy-redshift catalog reflection (unblocks Alembic) (#14)
- **Commitizen**: Service(+/~/-) commit format and Keep a Changelog (#12)

### Changed

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
