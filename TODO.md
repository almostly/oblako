# oblako-ml — TODO

Deferred features, with enough detail to pick up later. Order is rough priority.

## 1. RDS + Aurora local — instances & clusters + db

- **[DONE] Core:** `RdsService` (`oblako.rds`, alias `oblako.aurora`) — a `postgres:16`
  engine container (port 5432, `rds_data` volume) + `get_client()` → boto3 `rds`
  (RDS `create_db_instance` and Aurora `create_db_cluster` via moto, with writer/reader
  endpoints + members) + `connect()` (psycopg2). Wired into platform/compose/CLI;
  unit + integration tests; `examples/10_rds_aurora.py`. **DB data persists** via the
  volume; cluster/instance **metadata is moto/in-memory** (recreate after restart).
- **[DONE] RDS Data API (`rds-data`):** `oblako_ml/rds_data/` — a synchronous executor
  over the engine (rest-json: `/Execute`, `/BatchExecute`, `/Begin|Commit|RollbackTransaction`)
  with real **transactions**, params, and `formatRecordsAs=JSON`, reusing the
  `redshift-data` Field encoding. `oblako.rds.get_data_client()` → boto3 `rds-data`;
  `oblako rds-data` runs it standalone (port 8006). Integration tests + README.
- **[DONE] MySQL engine option:** `RdsService(engine="mysql")` runs a `mysql:8.0`
  engine on 3306; `connect()` uses PyMySQL (optional `[mysql]` extra). Control plane
  is engine-agnostic (`aurora-mysql` works). `rds-data` stays Postgres-only.
- **[DONE] Seed helper:** `RdsService.seed(instances=[...], clusters=[...])`
  idempotently recreates moto cluster/instance metadata after a restart.
- **[DONE] rds-data over MySQL:** `RdsDataExecutor(engine="mysql")` (PyMySQL +
  MySQL FIELD_TYPE name mapping); shares the Field encoding, binding, and
  transaction logic with the Postgres path. `RdsService(engine="mysql").get_data_client()`
  serves it; the standalone server honors `OBLAKO_RDS_ENGINE=mysql`.
- Note: Aurora **DSQL** (distributed SQL) has no local option — skip.

## Persistence cleanup (cross-cutting)

What persists today: engine volumes (Bedrock/Ollama, OpenSearch, Redshift/pgredshift,
S3Proxy). What does NOT: moto control-plane state (clusters/instances — in-memory),
our in-process stores (`redshift-data` statement history, bedrock batch `JobStore` —
though batch *outputs* go to S3), and Step Functions (in-memory).

- **[DONE] Fix DynamoDB persistence:** added `command: -jar DynamoDBLocal.jar -sharedDb
  -dbPath ./data` + `working_dir: /home/dynamodblocal` + `user: root` (the named volume
  is root-owned; the default non-root user can't write `shared-local-instance.db`).
  Applied to `docker-compose.yml` and `DynamoDBService`; verified a table + item survive
  a container recreate.
- **(Optional) control-plane persistence:** moto OSS has no on-disk persistence. If
  wanted, add a tiny disk-dump/reload for our own stores (statement history, jobs)
  and a seed helper for moto-defined clusters/instances. Low priority — data already
  persists; metadata is cheap to recreate.

## 2. OpenRouter backend for Bedrock — [DONE]

`BedrockAdapter` now has a pluggable backend (`oblako_ml/bedrock/backends.py`):
`OllamaBackend` (default, offline) and `OpenRouterBackend` (openrouter.ai
OpenAI-compatible API with `OPENROUTER_API_KEY`). Selected via
`OBLAKO_BEDROCK_BACKEND=ollama|openrouter`. Bedrock ids map to OpenRouter slugs
(`resolve_openrouter`); raw slugs / `openrouter.<slug>` pass through. Unit tests
mock the HTTP call (no live key needed).

## 3. Redshift ML

- **[DONE] Framework + `MODEL_TYPE LINEAR_LEARNER`:** `CREATE MODEL` SQL is intercepted
  in the `redshift-data` executor (`oblako_ml/redshift_ml/`), trains in a real SageMaker
  local container (scikit-learn), stores coefficients in `_ml_models`, and generates a
  pure-Python `plpython3u` predict UDF. Regression + binary classification, verified.
- **[DONE] `MODEL_TYPE MLP` and `XGBOOST`:** training (sklearn MLP with scaling /
  xgboost) in the SageMaker container + pure-Python in-DB inference (MLP forward pass,
  XGBoost tree-walk over the booster dump). XGBoost accepts Redshift's
  `AUTO OFF / OBJECTIVE / HYPERPARAMETERS` syntax. All three types verified for
  regression + binary classification (6 parametrized integration tests).
- **[DONE] Autopilot `AUTO ON` + multiclass:** with no `MODEL_TYPE`, Autopilot trains
  all three types, scores each on a holdout split (accuracy / R²), and keeps the best
  (`AUTO OFF`/`MODEL_TYPE` pins one); the result carries a `leaderboard` + `selected`.
  Multiclass classification works for all three types (`PROBLEM_TYPE
  multiclass_classification`, `OBJECTIVE 'multi:*'`, or auto-detected from a small
  non-negative-integer target) — pure-Python in-DB inference does per-class argmax
  (linear scores / MLP forward pass / per-class XGBoost tree sums). Unit tests
  (`tests/test_redshift_ml_unit.py`) + parametrized integration tests
  (`tests/test_redshift_ml.py`); `examples/12_redshift_ml.py` has a multiclass +
  Autopilot demo.

## 4. CloudFormation (declarative *resource* provisioning) — [DONE]

The **scoped, real version** (not LocalStack's CFN, not moto's mock-backed CFN):
`oblako_ml/cloudformation/` — a query/XML server speaking the real `cloudformation`
wire protocol, so a boto3 client, `aws cloudformation deploy`, and `sam deploy`
(via `AWS_ENDPOINT_URL_CLOUDFORMATION`) all work and provision into oblako's
**real engines**.

- **engine.py** — parses JSON + YAML templates (CFN short tags `!Ref`/`!GetAtt`/
  `!Sub`/`!Join` via a custom loader), resolves core intrinsics + pseudo params +
  parameter **defaults**, orders resources by `DependsOn`/`Ref` (topological), and
  holds an in-memory `StackStore` (create-change-set → describe → execute → delete).
- **providers.py** — per-type resource-provider registry dispatching to real engines:
  `AWS::S3::Bucket`→S3Proxy, `AWS::DynamoDB::Table`→DynamoDB Local,
  `AWS::StepFunctions::StateMachine`→Step Functions Local,
  `AWS::OpenSearchService::Domain`→OpenSearch (shared engine; the domain is a
  handle, delete is a no-op), `AWS::Redshift::Cluster`/`AWS::RDS::DBInstance`→moto.
  Providers may return `{"PhysicalId", "Attributes"}` so `Fn::GetAtt` is
  attribute-aware (e.g. StateMachine `Name`/`Arn`, Domain `DomainEndpoint`).
- **app.py** — Starlette, single POST `/`, parses the form-encoded query body,
  dispatches on `Action`, hand-rolled XML responses; `DescribeStacks` returns
  `ValidationError` for missing stacks (drives the CLI's create path). Resolves a
  change set by name **or** Id/ARN (the CLI calls Describe/Execute with the ARN).
- **transform.py** — the **SAM transform** (`AWS::Serverless-2016-10-31`) is expanded
  server-side: `AWS::Serverless::SimpleTable`→`DynamoDB::Table` (real, DynamoDB Local),
  `AWS::Serverless::Function`→`AWS::Lambda::Function` + implicit `AWS::IAM::Role`
  (provisioned into **moto** — describable, placeholder code; invoke via `sam local`),
  `AWS::Serverless::Api`→`ApiGateway::RestApi` (moto). New providers: Lambda, IAM Role,
  ApiGateway RestApi.
- Wired: `CloudFormationService` (`oblako.cloudformation`, in-process — no container,
  hidden from `status`), `oblako cloudformation` CLI (port 5601),
  `examples/13_cloudformation.py`, unit + gated integration tests
  (`tests/test_cloudformation.py`). Verified end-to-end with **real `aws
  cloudformation deploy`** (plain template: default + `--parameter-overrides` + env-var
  endpoint; SAM template: function+role→moto, SimpleTable→DynamoDB) and teardown.
- A function's `Api`/`HttpApi` event now expands to an implicit `ServerlessRestApi`
  (`AWS::ApiGateway::RestApi`) record in moto when no API is declared.
- **`sam deploy` verified end-to-end** (package → change set → SAM-transform expansion
  → moto), which drove two CFN-server fidelity fixes now in place: **stack-level
  lifecycle events** (`REVIEW_IN_PROGRESS` on change set, terminal `CREATE_COMPLETE` —
  `sam deploy` reads `StackEvents[0]`) and **`TemplateURL` support** (`CreateChangeSet`
  fetches the S3-uploaded template via S3Proxy when no `TemplateBody`). Checksum is
  handled with `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` + S3 path-style.
- **Recommended path is `aws cloudformation deploy`** (no packaging). The one-command
  `sam deploy` is blocked only by S3Proxy: SAM hardcodes `x-amz-server-side-encryption:
  AES256` on artifact upload and S3Proxy returns 501 (no SSE) — same class of S3Proxy
  gap as the declined MinIO (see below). Decided: document `aws cloudformation deploy`
  as the clean path rather than add an SSE-stripping shim.
- **Not done (later layer):** API method/integration wiring + `Lambda::Permission`
  (the RestApi record exists but isn't wired to the function — the live API is
  `sam local start-api`, see `examples/sam/`); no Lambda *runtime* (functions run via
  `sam local`). Stack metadata is in-memory (lives with the running server);
  provisioned resources persist.

## Considered & declined (revisit only on request)

- **MinIO as the S3 backend** — would give real CRC32 / flexible-checksum support
  *and SSE* (S3Proxy implements neither — SSE is why one-command `sam deploy` packaging
  fails; see CloudFormation above). Declined to keep the lightweight "S3 over local
  filesystem" model; uploads use `request_checksum_calculation="when_required"`
  instead. See `oblako_ml/services/s3proxy.py`. Revisit only if real S3 feature
  fidelity (checksums/SSE) becomes a hard requirement.
