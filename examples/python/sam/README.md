# SAM + oblako — Lambda functions that use oblako's services

This runs a **Lambda function locally with AWS SAM** and has it read/write
**oblako's local AWS services** (S3Proxy + DynamoDB Local). It's the "serverless
execution" half of the story — `sam local` runs your *functions*, and they hit
oblako for the *services*. Invoke the function directly (`sam local invoke`) or
front it with a local **API Gateway** (`sam local start-api`, see below).

## Build with uv

This function is built with **uv, not pip** — `template.yaml` sets
`Metadata: BuildMethod: python-uv` and dependencies live in `pyproject.toml`
(here, `shortuuid`). `BuildMethod: python-uv` is a SAM **beta** feature, so opt in
with `--beta-features` (or `SAM_CLI_BETA_PYTHON_UV=1`); it needs a SAM CLI new
enough to ship the `PythonUvBuilder` (≈ 1.161+) and `uv` on PATH.

```bash
# 1. oblako services up (S3Proxy :9000, DynamoDB Local :8001)
oblako up s3 && oblako up dynamodb             # or: make up

# 2. build the function with uv, then invoke it (needs SAM CLI + Docker)
sam build -t examples/sam/template.yaml --beta-features      # Running PythonUvBuilder:...
sam local invoke OblakoFn -e examples/sam/event.json
```

> If your on-PATH `sam` is too old for `python-uv` (or a brew bottle is broken),
> run a clean one with uv: prefix the commands with
> `uvx --python 3.12 --from aws-sam-cli` (e.g.
> `SAM_CLI_BETA_PYTHON_UV=1 uvx --python 3.12 --from aws-sam-cli sam build …`).

Expected output:

```json
{"s3_roundtrip": "hello from SAM local, stored in oblako",
 "record_id": "SMnbEpnLrQrxudzbnkiqaS",
 "ddb_item": {"id": "demo.txt", "body": "...", "rid": "SMnbEp...", "ts": "..."}}
```

(`record_id` comes from `shortuuid`, the uv-installed dependency — proof the uv
build bundled it.)

The object/item are really in oblako (verify from the host):

```python
from oblako.services import S3ProxyService, DynamoDBService
S3ProxyService().get_client().get_object(Bucket="sam-oblako", Key="demo.txt")
DynamoDBService(host_port=8001).get_client().get_item(TableName="SamOblako", Key={"id": {"S": "demo.txt"}})
```

## API Gateway — `sam local start-api`

This is how you use **API Gateway with oblako**: there's no oblako-native gateway —
`sam local start-api` *is* the local API Gateway, and the function behind it uses
oblako's services. The template's function declares two routes (`Api` events):

```yaml
Events:
  PutItem: { Type: Api, Properties: { Path: /items/{id}, Method: put } }
  GetItem: { Type: Api, Properties: { Path: /items/{id}, Method: get } }
```

Build once, then serve the API (it reads the build output, so dependencies like
`shortuuid` are present):

```bash
sam build --beta-features                  # from examples/sam/ (uv backend)
sam local start-api --port 3000            # local API Gateway on :3000
```

Hit it — the request routes to the Lambda, which reads/writes oblako for real:

```bash
curl -X PUT http://localhost:3000/items/apigw-demo --data 'stored via API Gateway'
# {"s3_roundtrip": "stored via API Gateway", "record_id": "…", "ddb_item": {…}}

curl http://localhost:3000/items/apigw-demo
# {"id": "apigw-demo", "body": "stored via API Gateway", "rid": "…", "ts": "…"}

curl http://localhost:3000/items/missing   # -> 404 {"error": "no item 'missing'"}
```

The request path is:

```
curl → sam local start-api (:3000) → Lambda container → boto3 → S3Proxy/DynamoDB on the host
```

The object/item are really in oblako afterward (verify from the host with the same
`S3ProxyService`/`DynamoDBService` calls shown above, `Key="apigw-demo"`).

> The control plane is separate: `aws cloudformation deploy` of this template at
> oblako's local CloudFormation registers the function/role (and any tables) as
> describable records in moto, but the **live** endpoint is always `start-api`.
> oblako has no Lambda/API Gateway *runtime* — that's SAM's job, by design.

## How the wiring works
- The Lambda runs in SAM's container; it reaches oblako on the host via
  **`host.docker.internal`**. The endpoints are injected as env vars in
  `template.yaml` (`S3_ENDPOINT`, `DDB_ENDPOINT`) and the handler points boto3 at
  them.
- The S3 client uses **path-style addressing + checksums-off** (S3Proxy doesn't
  implement the new CRC32/aws-chunked uploads).
- **Step Functions** ties in too: Step Functions Local is pre-wired to call SAM
  Lambda at `host.docker.internal:3001` — run `sam local start-lambda --port 3001`
  and your state machines can invoke these functions.

## What SAM does and doesn't do here
- ✅ `sam local invoke` / `start-api` / `start-lambda` run your **functions**
  locally; they use oblako's services for real.
- ✅ `sam deploy` (and `aws cloudformation deploy`) **can now provision the
  template's *resources* into oblako** — point them at oblako's local
  CloudFormation:

  ```bash
  oblako cloudformation                                   # server on :8017
  export AWS_ENDPOINT_URL_CLOUDFORMATION=http://localhost:8017
  sam deploy --stack-name demo --no-confirm-changeset     # plain CFN resource types
  ```

  An `AWS::S3::Bucket` lands in S3Proxy, an `AWS::DynamoDB::Table` in DynamoDB
  Local, etc. (supported: S3::Bucket, DynamoDB::Table, Redshift::Cluster,
  RDS::DBInstance). See `examples/python/cloudformation/deploy.py` and the README.
- ✅ The SAM **transform** (`AWS::Serverless-2016-10-31`) is expanded by the local
  CloudFormation. `AWS::Serverless::SimpleTable` becomes a **real** DynamoDB Local
  table; `AWS::Serverless::Function` becomes `AWS::Lambda::Function` + its implicit
  `AWS::IAM::Role`, **registered in moto** (describable via `aws lambda get-function`).
  So `sam deploy` of *this* template provisions `OblakoFn` (+ its role) into moto.
- ❌ oblako has no Lambda runtime — the moto function holds a placeholder, so you
  still **invoke** the function via `sam local`. Event sources (implicit APIs,
  `Lambda::Permission`) aren't wired.
- ⚠️ **`aws cloudformation deploy` is the clean declarative path** (no packaging
  step). `sam deploy` additionally *packages* code to S3 first: the checksum part is
  solvable (`export AWS_REQUEST_CHECKSUM_CALCULATION=when_required` + S3 path-style),
  but SAM also hardcodes `x-amz-server-side-encryption: AES256` on upload and S3Proxy
  returns 501 for SSE — so full `sam deploy` packaging needs an SSE-capable S3. The
  local CFN itself (change sets, SAM-transform expansion, `TemplateURL` fetch) is
  verified working with `sam deploy`; only S3Proxy's missing SSE blocks the one-command
  flow.

SAM is an external tool — install it yourself (it's not bundled with oblako).
