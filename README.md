# oblako-ml

[![CI](https://github.com/almostly/oblako/actions/workflows/ci.yml/badge.svg)](https://github.com/almostly/oblako/actions/workflows/ci.yml)

Local AWS ML platform. Run Bedrock, SageMaker, Step Functions, and more on your laptop, no cloud required.

Unlike LocalStack, oblako-ml wires together **real local modes** of AWS services and open-source alternatives:

| AWS Service | Local replacement | How |
|---|---|---|
| Bedrock (LLMs) | Ollama (or OpenRouter) | boto3 `bedrock-runtime` invoke/converse; OpenRouter backend hits real models with your key |
| Bedrock (control plane) | oblako-ml server | boto3 `bedrock`: foundation-model catalog + batch model-invocation jobs |
| Bedrock (embeddings) | Ollama + nomic-embed-text | Vector embeddings for RAG |
| Bedrock Agents | Ollama + SAM local | Agent loop with local tool calls |
| Bedrock AgentCore (Runtime) | bedrock-agentcore SDK | Local agent on the `/invocations` + `/ping` contract |
| Bedrock Knowledge Bases | OpenSearch | Vector search with k-NN |
| SageMaker | SDK local mode | `instance_type="local"` uses Docker |
| Step Functions | aws-stepfunctions-local | Official AWS Docker image |
| Lambda | AWS SAM CLI (external) | `sam local invoke` — oblako doesn't manage it; bring your own SAM CLI |
| API Gateway | AWS SAM CLI (external) | `sam local start-api` — real local API Gateway routing HTTP to your functions (which use oblako's services); see `examples/sam/` |
| S3 | S3Proxy | S3 API over local filesystem |
| DynamoDB | dynamodb-local | Official AWS Docker image |
| Redshift (storage) | pgredshift | PostgreSQL 10 + Redshift system tables, `SET query_group`, and UDFs |
| Redshift (management API) | moto | boto3 `redshift` control plane: clusters, nodes, endpoints |
| Redshift Data API | oblako-ml server | boto3 `redshift-data`, executes real SQL against pgredshift |
| Redshift ML | SageMaker local + plpython3u | `CREATE MODEL` trains in a real container; predict UDF runs in-DB |
| RDS | moto + PostgreSQL | boto3 `rds` control plane (instances) + real Postgres engine |
| Aurora | moto + PostgreSQL | boto3 `rds` clusters (writer/reader endpoints) + real Postgres engine |
| RDS Data API | oblako-ml server | boto3 `rds-data`: synchronous SQL + transactions against the engine |
| CloudFormation | oblako-ml server | boto3 `cloudformation` (+ `aws cloudformation deploy` / `sam deploy`): templates provision **real** oblako resources |
| AppConfig | oblako-ml agent | Python reimplementation |

## Quick start

```bash
oblako up              # start all services
oblako pull qwen2.5:0.5b   # pull a model into the engine
oblako dashboard       # open web dashboard at http://localhost:8000
```

## CLI commands

```
oblako up [service]        Start all services (or a specific one)
oblako down [service]      Stop all services (or a specific one)
oblako status              Show service status
oblako dashboard [-p PORT] Start the web dashboard (default: port 8000)
oblako redshift-data [-p PORT] Start the Redshift Data API server (default: port 8002)
oblako bedrock-runtime [-p PORT] Start the Bedrock Runtime server (default: port 8004)
oblako rds-data [-p PORT]  Start the RDS Data API server (default: port 8006)
oblako cloudformation [-p PORT]  Start the CloudFormation server (default: port 5601)
oblako agentcore run <file>  Run a local AgentCore agent (default: port 8080)
oblako agentcore invoke <json>  Invoke a running AgentCore agent
oblako logs <service>      Show logs for a service
oblako pull [model]        Pull a model into the engine (default: qwen2.5:0.5b)
oblako models              List available Ollama models
oblako test                Run unit tests
oblako test-integration    Run integration tests (requires services running)
```

### Full workflow

```bash
# Start
oblako up                  # start all Docker services
oblako pull qwen2.5:0.5b   # pull a model
oblako dashboard           # start dashboard + API on http://localhost:8000

# Stop
Ctrl+C                     # stop the dashboard
oblako down                # stop all Docker services
```

### Individual services

```bash
oblako up bedrock          # start just Bedrock (Ollama engine)
oblako up redshift         # start just Redshift
oblako logs opensearch     # tail OpenSearch logs
oblako down stepfunctions  # stop just Step Functions
```

Available service names: `bedrock`, `opensearch`, `redshift`, `rds`, `moto`, `s3`, `dynamodb`, `stepfunctions` (`ollama` aliases `bedrock`; `aurora` aliases `rds`)

## Dashboard

The web dashboard uses AWS Cloudscape Design System (the same components as the real AWS Console). Start it with `oblako dashboard`, then open http://localhost:8000.

Pages:
- **Services** - status overview of all running services
- **Notebook** - Python code editor with syntax highlighting, run code against all local services
- **Bedrock** - chat playground powered by Ollama
- **SageMaker** - training jobs, endpoints, Docker images, cleanup
- **S3** - bucket browser with object listing
- **DynamoDB** - table browser with item viewer
- **Step Functions** - state machines, ASL JSON viewer, execution history, flow diagram
- **CloudFormation** - stacks deployed to the local CloudFormation, with resources, outputs, and events
- **Redshift** - cluster list (management API), table list, and SQL query editor with results

## Services

### Bedrock (via Ollama, or OpenRouter)

oblako surfaces a chat **backend** as **Bedrock** and translates `invoke_model` / `converse` to it. Two backends, chosen by env:

| `OBLAKO_BEDROCK_BACKEND` | backend | notes |
|---|---|---|
| `ollama` (default) | local Ollama | fully offline; Bedrock id → local model |
| `openrouter` | [openrouter.ai](https://openrouter.ai) | **real frontier models with your key** — `export OPENROUTER_API_KEY=…`; Bedrock id → OpenRouter slug (e.g. `anthropic.claude-3-5-sonnet-…` → `anthropic/claude-3.5-sonnet`) |

The OpenRouter backend lets you test Bedrock-style code (`invoke_model`/`converse`) against real models without an AWS account. The difference that matters: with **Ollama every Bedrock id collapses to the one local model**, whereas with **OpenRouter each Bedrock id routes to its real counterpart**. The full Bedrock text catalog is mapped — Claude, Llama (2/3/3.1/3.2), Mistral/Mixtral, Cohere Command, AI21 Jamba, and Amazon Nova (Titan text → Nova). Where OpenRouter has retired an exact Bedrock version (e.g. `claude-3.5-sonnet`, Llama 2, Mixtral 8x7B, Jurassic), it maps to the **nearest current model in the same family**; context-length variants (`…:200k`) normalize automatically. Pass a mapped Bedrock id, a raw slug, or `openrouter.<slug>`. (Embeddings and image models have no chat equivalent → clear error.)

The boto3-compatible way — a real `bedrock-runtime` client against the local endpoint (port 8004; `oblako.bedrock.get_client()` auto-starts the server):

```python
from oblako_ml.services import BedrockService

br = BedrockService().get_client()   # boto3.client("bedrock-runtime")

resp = br.converse(
    modelId="qwen2.5:0.5b",
    messages=[{"role": "user", "content": [{"text": "Hello"}]}],
    inferenceConfig={"maxTokens": 256},
)
print(resp["output"]["message"]["content"][0]["text"])

import json
resp = br.invoke_model(
    modelId="anthropic.claude-3-haiku-20240307-v1:0",
    body=json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "Hello"}],
    }),
)
print(json.loads(resp["body"].read())["content"][0]["text"])
```

Or call the translation layer (`BedrockAdapter`) directly, without the HTTP server:

```python
from oblako_ml.bedrock.adapter import BedrockAdapter

adapter = BedrockAdapter()
result = adapter.converse(
    model_id="anthropic.claude-3-haiku-20240307-v1:0",
    messages=[{"role": "user", "content": [{"text": "Hello"}]}],
    inference_config={"maxTokens": 256},
)
print(result["output"]["message"]["content"][0]["text"])
```

Run the Bedrock Runtime server standalone (for boto3 clients in other processes):

```bash
oblako bedrock-runtime          # serves boto3 'bedrock-runtime' (+ 'bedrock' control plane) on :8004
```

### Bedrock control plane (foundation models + batch inference)

The same server also speaks the `bedrock` control plane. `oblako.bedrock.get_control_client()` returns a boto3 `bedrock` client.

```python
bedrock = BedrockService().get_control_client()   # boto3.client("bedrock")

# Foundation-model catalog (real Bedrock IDs + your locally-available Ollama models)
for m in bedrock.list_foundation_models()["modelSummaries"]:
    print(m["modelId"], m["providerName"])
bedrock.get_foundation_model(modelIdentifier="anthropic.claude-3-5-sonnet-20241022-v2:0")

# Pass a model directly with the `ollama.` prefix (or just the raw name)
br.converse(modelId="ollama.qwen2.5:0.5b", messages=[...])
```

**Batch inference** runs JSONL records from S3 (S3Proxy) through the engine and writes results back to S3 — like real Bedrock batch jobs:

```python
# input JSONL: one {"recordId", "modelInput"} per line in s3://bucket/in/
job = bedrock.create_model_invocation_job(
    jobName="score-batch", roleArn="arn:aws:iam::000000000000:role/Dummy",
    modelId="qwen2.5:0.5b",
    inputDataConfig={"s3InputDataConfig": {"s3Uri": "s3://my-in/in/"}},
    outputDataConfig={"s3OutputDataConfig": {"s3Uri": "s3://my-out/out/"}},
)
bedrock.get_model_invocation_job(jobIdentifier=job["jobArn"])["status"]  # Submitted -> InProgress -> Completed
# output JSONL ({"recordId","modelInput","modelOutput"}) lands under s3://my-out/out/<jobId>/
```

### Bedrock AgentCore (local runtime)

The AgentCore Runtime contract — `POST /invocations` + `GET /ping` — runs locally with the `bedrock-agentcore` SDK. Write an agent, point it at local Bedrock, and run it offline. Install the extra: `pip install 'oblako-ml[agentcore]'`.

```python
# my_agent.py
import boto3
from oblako_ml.agentcore import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
bedrock = boto3.client("bedrock-runtime", endpoint_url="http://localhost:8004",
                       region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")

@app.entrypoint
def handler(payload):
    r = bedrock.converse(modelId="qwen2.5:0.5b",
        messages=[{"role": "user", "content": [{"text": payload["prompt"]}]}])
    return {"reply": r["output"]["message"]["content"][0]["text"]}
```

```bash
oblako bedrock-runtime &                       # local Bedrock on :8004
oblako agentcore run my_agent.py               # serves the agent on :8080
oblako agentcore invoke '{"prompt": "Hi"}'     # POST /invocations
```

See `examples/08_agentcore_agent.py`. (Only the AgentCore *Runtime* is local; Gateway/Memory/Identity remain managed services.)

### SageMaker (local mode)

SageMaker local mode (`instance_type="local"`) launches a **real Docker container** that trains your code — the SDK even generates a throwaway `docker-compose.yml` per job. It's not a long-running service, so it isn't in `docker-compose.yml`; `SageMakerService` provides helpers (build images, list/cleanup `sagemaker-local-*` containers) and a `LocalSession` factory. Install the extra:

```bash
pip install 'oblako-ml[sagemaker]'    # pins the v2 SDK (v3 dropped local mode)
```

Fully local — no S3, no ECR — using a bring-your-own-container image and `file://` paths:

```python
from sagemaker.estimator import Estimator
from oblako_ml.services import SageMakerService

sm = SageMakerService()
sm.build_image(path="examples/sagemaker", tag="oblako-sagemaker-train:latest")  # build locally

estimator = Estimator(
    image_uri="oblako-sagemaker-train:latest",
    role="arn:aws:iam::000000000000:role/dummy",
    instance_count=1, instance_type="local",
    sagemaker_session=sm.get_session(),
    output_path="file:///tmp/sm-out",
)
estimator.fit({"train": "file:///tmp/sm-train"})   # real container trains; writes model.tar.gz

sm.list_training_containers()   # docker-py view of sagemaker-local-* containers
sm.cleanup()                    # remove stopped ones
```

See `examples/11_sagemaker_local.py` (end-to-end: builds the image, trains `y = 2x + 1` in a real container, reads back the model artifact).

### S3 (via S3Proxy)

S3Proxy runs on port 9000. Point boto3 at it:

```python
import boto3

s3 = boto3.client("s3", endpoint_url="http://localhost:9000")
s3.create_bucket(Bucket="my-bucket")
s3.put_object(Bucket="my-bucket", Key="data.csv", Body=b"a,b,c\n1,2,3")
```

### DynamoDB (local)

DynamoDB Local runs on port 8001:

```python
import boto3

ddb = boto3.client("dynamodb", endpoint_url="http://localhost:8001",
    aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
ddb.create_table(
    TableName="MyTable",
    KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
    BillingMode="PAY_PER_REQUEST",
)
```

### Redshift (via pgredshift)

Redshift comes in three layers, all local and boto3-compatible:

**1. Storage engine (pgredshift).** Runs on port 5439 (Redshift's port) with user `oblako`, password `oblako`, database `oblako`. It's `hearthsim/pgredshift` — PostgreSQL 10 plus Redshift system tables (`stl_scan`, `stv_tbl_perm`, ...), `SET query_group`, and Redshift UDFs (`json_array_length`, `median`, ...). Connect with psycopg2:

```python
import psycopg2

conn = psycopg2.connect(
    host="localhost", port=5439,
    user="oblako", password="oblako", dbname="oblako",
)
```

**2. Management API (control plane).** Clusters, nodes, endpoints, snapshots — served by a local `motoserver/moto` container on port 5500. Use a real boto3 `redshift` client:

```python
from oblako_ml.services import RedshiftService

redshift = RedshiftService().get_client()   # boto3.client("redshift")
redshift.create_cluster(
    ClusterIdentifier="credit-dw", NodeType="ra3.xlplus", NumberOfNodes=2,
    MasterUsername="oblako", MasterUserPassword="Oblako123", DBName="oblako",
)
cluster = redshift.describe_clusters(ClusterIdentifier="credit-dw")["Clusters"][0]
print(cluster["NumberOfNodes"], cluster["NodeType"], cluster["ClusterStatus"])
```

**3. Redshift Data API (data plane).** Run SQL over HTTP — but unlike moto's mock, statements execute **for real** against the pgredshift container and return real rows. Served on port 8002. Use a real boto3 `redshift-data` client:

```python
rd = RedshiftService().get_data_client()     # boto3.client("redshift-data"); auto-starts the server

q = rd.execute_statement(
    ClusterIdentifier="credit-dw", Database="oblako",
    Sql="SELECT segment, count(*) FROM customer_scores GROUP BY segment",
)
rd.describe_statement(Id=q["Id"])["Status"]   # "FINISHED"
rd.get_statement_result(Id=q["Id"])["Records"]  # real rows in Field format

# named parameters, like the real API
rd.execute_statement(Database="oblako",
    Sql="SELECT * FROM customer_scores WHERE segment = :seg",
    Parameters=[{"name": "seg", "value": "prime"}])
```

For an external process (not the one that called `get_data_client`), run the Data API server standalone:

```bash
oblako redshift-data            # serves boto3 'redshift-data' on http://localhost:8002
```

### Redshift ML (CREATE MODEL)

Real `CREATE MODEL` SQL, fully local: the `redshift-data` server intercepts it, exports the `FROM (SELECT …)` rows, **trains in a real SageMaker local container** (scikit-learn), stores the coefficients, and generates a **`plpython3u` prediction UDF** in pgredshift. Then `SELECT my_predict(...)` is real in-database inference. Needs `pip install 'oblako-ml[sagemaker]'` + Docker.

```sql
CREATE MODEL price_model
  FROM (SELECT sqft, beds, price FROM homes)
  TARGET price FUNCTION predict_price
  MODEL_TYPE LINEAR_LEARNER;          -- regression (default)

SELECT sqft, beds, predict_price(sqft, beds) FROM homes;   -- in-DB inference
```

Binary **and multiclass** classification work too — `PROBLEM_TYPE binary_classification | multiclass_classification` (or auto-detected from the target: `{0,1}` → binary, a small set of non-negative integers → multiclass). The UDF returns the predicted class:

```sql
CREATE MODEL species_model
  FROM (SELECT petal, sepal, species FROM plants)   -- species in {0, 1, 2}
  TARGET species FUNCTION predict_species
  PROBLEM_TYPE multiclass_classification;            -- or just omit it (auto-detected)
```

**Autopilot** (`AUTO ON`, the default when no `MODEL_TYPE` is given) trains all three model types, scores each on a holdout split, and keeps the best; `MODEL_TYPE` (or `AUTO OFF`) pins a single type:

```sql
CREATE MODEL best_model
  FROM (SELECT sqft, beds, price FROM homes)
  TARGET price FUNCTION predict_best;   -- trains LINEAR_LEARNER + MLP + XGBOOST, keeps the winner
```

All three of Redshift ML's supervised model types work — for regression, binary, and multiclass — trained in the SageMaker container, exported to a pure-Python UDF (pgredshift's `plpython3u` has no numpy/sklearn/xgboost):

| `MODEL_TYPE` | trained with | in-DB inference UDF |
|---|---|---|
| `LINEAR_LEARNER` | scikit-learn linear/logistic | dot product (+ sigmoid; per-class argmax for multiclass) |
| `MLP` | scikit-learn MLP (scaled) | forward pass (argmax over outputs for multiclass) |
| `XGBOOST` | xgboost | tree-walk over the booster dump (per-class sum + argmax for multiclass) |

XGBoost accepts Redshift's `AUTO OFF MODEL_TYPE xgboost OBJECTIVE 'reg:squarederror'|'binary:logistic' HYPERPARAMETERS … (NUM_ROUND, MAX_DEPTH)` syntax. See `examples/12_redshift_ml.py`. (Syntax/use cases follow [aws-samples/amazon-redshift-ml-getting-started](https://github.com/aws-samples/amazon-redshift-ml-getting-started); the local training + inference are oblako's.)

### RDS / Aurora

Same shape as Redshift: a real PostgreSQL engine (port 5432) for the data plane, and moto for the `rds` control plane. `oblako.rds` (alias `oblako.aurora`) gives you both — real SQL behavior, simulated cluster/instance topology.

```python
from oblako_ml.services import RdsService

svc = RdsService()
rds = svc.get_client()           # boto3.client("rds")

# RDS: a standalone instance
rds.create_db_instance(
    DBInstanceIdentifier="app-db", Engine="postgres", DBInstanceClass="db.t3.micro",
    MasterUsername="oblako", MasterUserPassword="Oblako123", AllocatedStorage=20, DBName="oblako",
)
rds.describe_db_instances(DBInstanceIdentifier="app-db")["DBInstances"][0]["Endpoint"]

# Aurora: a cluster with a writer (writer + reader endpoints, members)
rds.create_db_cluster(
    DBClusterIdentifier="analytics", Engine="aurora-postgresql",
    MasterUsername="oblako", MasterUserPassword="Oblako123", DatabaseName="oblako",
)
rds.create_db_instance(
    DBInstanceIdentifier="analytics-1", DBClusterIdentifier="analytics",
    Engine="aurora-postgresql", DBInstanceClass="db.r6g.large",
)
rds.describe_db_clusters(DBClusterIdentifier="analytics")["DBClusters"][0]["ReaderEndpoint"]

# Data plane: real SQL against the engine
conn = svc.connect()             # psycopg2 connection (port 5432)
```

**RDS Data API (`rds-data`)** — a real boto3 `rds-data` client (port 8006; `get_data_client()` auto-starts the server). Unlike `redshift-data` it's **synchronous** (ExecuteStatement returns rows directly) and supports **transactions**:

```python
rd = svc.get_data_client()       # boto3.client("rds-data")
arn = dict(resourceArn="arn:aws:rds:us-east-1:0:cluster:analytics",
           secretArn="arn:aws:secretsmanager:us-east-1:0:secret:db", database="oblako")

r = rd.execute_statement(sql="SELECT id, name FROM widgets ORDER BY id",
                         includeResultMetadata=True, **arn)
r["records"]            # real rows in Field format, returned immediately

# transactions
tx = rd.begin_transaction(resourceArn=arn["resourceArn"], secretArn=arn["secretArn"], database="oblako")["transactionId"]
rd.execute_statement(sql="INSERT INTO widgets VALUES (:id, :n)",
    parameters=[{"name": "id", "value": {"longValue": 9}}, {"name": "n", "value": {"stringValue": "z"}}],
    transactionId=tx, **arn)
rd.commit_transaction(resourceArn=arn["resourceArn"], secretArn=arn["secretArn"], transactionId=tx)
```

See `examples/10_rds_aurora.py`. Run the Data API server standalone with `oblako rds-data`.

**Engine choice.** `RdsService(engine="mysql")` runs a MySQL 8 engine instead of Postgres (`connect()` then uses PyMySQL — install `pip install 'oblako-ml[mysql]'`). The control plane is engine-agnostic (`create_db_cluster(Engine="aurora-mysql")` works), and **`rds-data` supports both engines** — `get_data_client()` runs SQL against whichever engine the `RdsService` uses.

**Seeding.** Control-plane objects live in moto (in-memory), so they vanish on restart. `RdsService().seed(...)` recreates them idempotently:

```python
RdsService().seed(
    instances=[{"DBInstanceIdentifier": "app-db", "Engine": "postgres",
                "DBInstanceClass": "db.t3.micro", "MasterUsername": "oblako",
                "MasterUserPassword": "Oblako123", "AllocatedStorage": 20}],
    clusters=[{"DBClusterIdentifier": "analytics", "Engine": "aurora-postgresql",
               "MasterUsername": "oblako", "MasterUserPassword": "Oblako123",
               "instances": [{"DBInstanceIdentifier": "analytics-1",
                              "Engine": "aurora-postgresql", "DBInstanceClass": "db.r6g.large"}]}],
)  # safe to re-run; only creates what's missing
```

### OpenSearch (for Knowledge Bases / RAG)

OpenSearch runs on port 9200 with security disabled for local use:

```python
from opensearchpy import OpenSearch

client = OpenSearch(
    hosts=[{"host": "localhost", "port": 9200}],
    use_ssl=False,
)
```

### Step Functions (local)

Step Functions Local runs on port 8083. It connects to SAM local Lambda on port 3001:

```bash
# Start SAM local in another terminal
sam local start-lambda --port 3001

# Create and run a state machine
aws stepfunctions create-state-machine \
    --endpoint-url http://localhost:8083 \
    --name my-workflow \
    --definition file://workflow.asl.json \
    --role-arn "arn:aws:iam::012345678901:role/DummyRole"
```

### Lambda (external — AWS SAM CLI)

oblako does **not** ship or manage Lambda. Lambda is run with the **AWS SAM CLI** (a separate install), which executes your function in a real local container. The Step Functions Local container is pre-wired to call it via `LAMBDA_ENDPOINT=http://host.docker.internal:3001`:

```bash
sam local start-lambda --port 3001    # you run this; oblako's Step Functions calls it
```

There's no `oblako.lambda` service — it's intentionally external (the same way the real local Lambda tool, SAM, is a standalone CLI).

**SAM functions can use oblako's services.** A `sam local invoke` / `start-api` Lambda reaches oblako on the host via `host.docker.internal` (point boto3 at oblako's ports). See `examples/sam/` — a Lambda that does a real S3 + DynamoDB round-trip against S3Proxy and DynamoDB Local. (`sam local` runs *functions*; to provision a template's *resources* into oblako, point `sam deploy` / `aws cloudformation deploy` at oblako's local CloudFormation — see below.)

### CloudFormation (declarative provisioning into oblako)

oblako runs a local **CloudFormation** that provisions resources into its *real* engines — an `AWS::S3::Bucket` lands in S3Proxy, an `AWS::DynamoDB::Table` in DynamoDB Local, an `AWS::StepFunctions::StateMachine` in Step Functions Local (not in a mock). It speaks the real `cloudformation` wire protocol, so a boto3 client, `aws cloudformation deploy`, and `sam deploy` all work. Supported types:

| Resource | Backed by |
|---|---|
| `AWS::S3::Bucket` | S3Proxy (real) |
| `AWS::DynamoDB::Table` | DynamoDB Local (real) |
| `AWS::StepFunctions::StateMachine` | Step Functions Local (real) |
| `AWS::OpenSearchService::Domain` | OpenSearch (real, shared engine — the domain is a handle; `DomainEndpoint` → local URL) |
| `AWS::Redshift::Cluster`, `AWS::RDS::DBInstance` | moto control plane |
| `AWS::Lambda::Function`, `AWS::IAM::Role`, `AWS::ApiGateway::RestApi` | moto (from the SAM transform; see below) |

`Fn::GetAtt` is attribute-aware where the engine exposes one (e.g. a state machine's `Name`/`Arn`, a domain's `DomainEndpoint`); other attributes fall back to the physical id.

```bash
oblako cloudformation                                   # start the server on :5601
export AWS_ENDPOINT_URL_CLOUDFORMATION=http://localhost:5601
aws cloudformation deploy --template-file template.yaml --stack-name demo
sam deploy --stack-name demo --no-confirm-changeset     # for plain CFN resource types
```

```python
from oblako_ml.services import CloudFormationService

cfn = CloudFormationService().get_client()   # boto3.client("cloudformation"); auto-starts the server
cfn.create_change_set(StackName="demo", TemplateBody=template, ChangeSetName="cs", ChangeSetType="CREATE")
cfn.execute_change_set(StackName="demo", ChangeSetName="cs")
cfn.get_waiter("stack_create_complete").wait(StackName="demo")
```

It parses JSON and YAML templates (including `!Ref`/`!GetAtt`/`!Sub`/`!Join` short tags), core intrinsics, parameter defaults, and `DependsOn`/`Ref` ordering. Stack metadata is in-memory (lives with the running server); the provisioned resources are real and persist in the engines.

The **SAM transform** (`AWS::Serverless-2016-10-31`) is expanded server-side, so `sam deploy` works: `AWS::Serverless::SimpleTable` → a real DynamoDB Local table; `AWS::Serverless::Function` → `AWS::Lambda::Function` + its implicit `AWS::IAM::Role` registered in moto (describable via `aws lambda get-function` / `aws iam get-role`). oblako has no Lambda runtime — the function record stores a placeholder; you still **invoke** functions via `sam local`. Event sources (implicit APIs, permissions) aren't wired. See `examples/13_cloudformation.py`.

> **Recommended path: `aws cloudformation deploy`** (or boto3) — it has no packaging step, so it provisions into oblako cleanly. `sam deploy` *packages* code to S3 first: the checksum issue is solvable (`export AWS_REQUEST_CHECKSUM_CALCULATION=when_required` + S3 path-style), but SAM also hardcodes `x-amz-server-side-encryption: AES256` on upload, which S3Proxy doesn't implement (501) — the same class of S3Proxy gap that led us to decline MinIO. So full `sam deploy` packaging needs an SSE-capable S3; the local CFN itself (change sets, the SAM transform, `TemplateURL`) is verified working with it.

## Python API

All services are also available programmatically:

```python
from oblako_ml.services import Oblako

oblako = Oblako()
oblako.up()                              # start everything
oblako.wait_ready()                      # block until healthy
print(oblako.status())                   # {'bedrock': 'running', ...}

s3 = oblako.s3.get_client()              # boto3 S3 client
br = oblako.bedrock.get_client()         # boto3 'bedrock-runtime' (-> Ollama)
conn = oblako.redshift.connect()         # psycopg2 connection to pgredshift
rs = oblako.redshift.get_client()        # boto3 'redshift' (clusters/nodes via moto)
rd = oblako.redshift.get_data_client()   # boto3 'redshift-data' (real SQL execution)
rds = oblako.rds.get_client()            # boto3 'rds' (RDS instances + Aurora clusters)
rdb = oblako.rds.get_data_client()       # boto3 'rds-data' (synchronous SQL + transactions)
dbconn = oblako.rds.connect()            # psycopg2 connection to the RDS/Aurora engine
sfn = oblako.stepfunctions.get_client()  # boto3 SFN client
ddb = oblako.dynamodb.get_client()       # boto3 DynamoDB client
cfn = oblako.cloudformation.get_client() # boto3 'cloudformation' (stacks -> real oblako resources)
oblako.bedrock.pull_model("qwen2.5:0.5b")  # pull a model into the engine

oblako.down()                            # stop everything
```

## Docker socket

SageMaker local mode and Ollama both need Docker. The docker-compose.yml mounts the Docker socket for Ollama. SageMaker local mode runs on the host and uses Docker directly.

## Tests

```bash
oblako test                # unit tests (no services needed)
oblako test-integration    # integration tests (requires oblako up)
```

## Roadmap

- **OpenRouter backend for Bedrock** — optional backend so `bedrock-runtime` routes to real frontier models via your OpenRouter key (test Bedrock-style code without AWS access). Ollama stays the offline default.
- **Redshift ML** — `CREATE MODEL` / `predict()` SQL wired to local SageMaker.

## Ports

| Service | Port |
|---|---|
| Bedrock engine (Ollama) | 11434 |
| Bedrock Runtime API | 8004 |
| AgentCore agent | 8080 |
| OpenSearch | 9200 |
| RDS / Aurora (PostgreSQL) | 5432 |
| RDS Data API | 8006 |
| Redshift (pgredshift) | 5439 |
| Redshift management API (moto) | 5500 |
| Redshift Data API | 8002 |
| S3Proxy | 9000 |
| DynamoDB Local | 8001 |
| Step Functions Local | 8083 |
| CloudFormation | 5601 |
| Dashboard | 8000 |
