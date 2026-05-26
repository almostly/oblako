"""SAM-local Lambda that uses oblako's local AWS services.

Works two ways, both reaching oblako on the host via `host.docker.internal`
(endpoints injected as env vars in template.yaml):
  * `sam local invoke -e event.json` — direct event {key, body}; does an
    S3 + DynamoDB round-trip and returns the stored record.
  * `sam local start-api` — a local API Gateway routes PUT/GET /items/{id} to
    this handler, which returns the API Gateway proxy response shape.
"""

import json
import os
import time

import boto3
import shortuuid  # installed by `sam build` (BuildMethod: python-uv), not the Lambda base image
from botocore.config import Config

CREDS = dict(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
BUCKET, TABLE = "sam-oblako", "SamOblako"


def _s3():
    # S3Proxy needs path-style + checksums off (it doesn't implement the new CRC32/aws-chunked).
    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT"],
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
        **CREDS,
    )


def _ddb():
    return boto3.client("dynamodb", endpoint_url=os.environ["DDB_ENDPOINT"], **CREDS)


def _ensure():
    s3, ddb = _s3(), _ddb()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except Exception:
        pass
    try:
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except Exception:
        pass
    return s3, ddb


def store(key, body):
    """Put the object + item in oblako; return the round-trip record."""
    s3, ddb = _ensure()
    s3.put_object(Bucket=BUCKET, Key=key, Body=body.encode())
    s3_roundtrip = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
    record_id = shortuuid.uuid()  # proves the uv-installed dependency is available
    ddb.put_item(TableName=TABLE, Item={
        "id": {"S": key}, "body": {"S": body},
        "rid": {"S": record_id}, "ts": {"N": str(int(time.time()))},
    })
    item = ddb.get_item(TableName=TABLE, Key={"id": {"S": key}})["Item"]
    return {
        "s3_roundtrip": s3_roundtrip,
        "record_id": record_id,
        "ddb_item": {k: list(v.values())[0] for k, v in item.items()},
    }


def fetch(key):
    """Read the item back from oblako's DynamoDB; flatten it (or None)."""
    _, ddb = _ensure()
    item = ddb.get_item(TableName=TABLE, Key={"id": {"S": key}}).get("Item")
    return {k: list(v.values())[0] for k, v in item.items()} if item else None


def _resp(status, payload):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(payload)}


def lambda_handler(event, context):
    # API Gateway (`sam local start-api`) sends a proxy event; direct invoke doesn't.
    if "httpMethod" in event or "requestContext" in event:
        method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method", "GET")
        key = (event.get("pathParameters") or {}).get("id", "hello.txt")
        try:
            if method == "PUT":
                return _resp(201, store(key, event.get("body") or ""))
            record = fetch(key)
            return _resp(200, record) if record else _resp(404, {"error": f"no item {key!r}"})
        except Exception as e:
            return _resp(500, {"error": str(e)})

    # direct invoke: `sam local invoke -e event.json`  ->  {key, body}
    return store(event.get("key", "hello.txt"), event.get("body", "hello from SAM + oblako"))
