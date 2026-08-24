"""Integration test: the Athena boto3 API runs SQL via Trino, results to S3.

Requires the Trino engine (+ its Iceberg/S3Proxy stack) reachable. oblako wraps
Trino in the Athena wire protocol: StartQueryExecution runs the SQL and stages
the results to the S3 OutputLocation, GetQueryResults returns the Athena
ResultSet — unmodified boto3 ``athena``. Override OBLAKO_TEST_S3_ENDPOINT to point
at an isolated S3Proxy.
"""

import os
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "athena-ci"


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        ),
    )


def test_athena_query_via_trino_to_s3():
    try:
        _s3().list_buckets()
        from oblako.services.trino import TrinoService

        if "error" in TrinoService().query("SELECT 1"):
            pytest.skip("Trino not ready")
    except Exception:
        pytest.skip("Trino or S3Proxy not available")

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.engines.athena import get_client

    s3 = _s3()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass

    athena = get_client()
    query_id = athena.start_query_execution(
        QueryString="SELECT 42 AS n, 'hi' AS s",
        ResultConfiguration={"OutputLocation": f"s3://{BUCKET}/results/"},
    )["QueryExecutionId"]

    state, desc = "QUEUED", {}
    for _ in range(60):
        desc = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
        state = desc["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.5)
    assert state == "SUCCEEDED", desc["Status"].get("StateChangeReason")

    results = athena.get_query_results(QueryExecutionId=query_id)["ResultSet"]
    rows = results["Rows"]
    # the first row is the column header
    assert [c["VarCharValue"] for c in rows[0]["Data"]] == ["n", "s"]
    assert [c["VarCharValue"] for c in rows[1]["Data"]] == ["42", "hi"]
    columns = results["ResultSetMetadata"]["ColumnInfo"]
    assert [c["Name"] for c in columns] == ["n", "s"]

    # the result set was also written to the OutputLocation as CSV
    out_key = desc["ResultConfiguration"]["OutputLocation"].split(f"{BUCKET}/", 1)[1]
    body = s3.get_object(Bucket=BUCKET, Key=out_key)["Body"].read().decode()
    assert body.splitlines()[0] == "n,s"
    assert "42,hi" in body
