"""Integration test: Athena queries S3 Tables through ``s3tablescatalog/<bucket>``.

Requires Trino, the Iceberg REST catalog and S3Proxy. A table created with the
``s3tables`` API is written and read through Athena, addressed the way AWS does:
by the query context (Catalog + Database) and by a fully qualified name.
"""

import contextlib
import os
import time
import uuid

import pytest

from oblako.engines import s3tables


def _wait(athena, query_id: str) -> dict:
    for _ in range(300):
        q = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
        if q["Status"]["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return q
        time.sleep(0.2)
    raise TimeoutError(query_id)


def _rows(athena, sql: str, **context) -> list[list[str | None]]:
    query_id = athena.start_query_execution(
        QueryString=sql, QueryExecutionContext=context
    )["QueryExecutionId"]
    q = _wait(athena, query_id)
    assert q["Status"]["State"] == "SUCCEEDED", q["Status"].get("StateChangeReason")
    rows = athena.get_query_results(QueryExecutionId=query_id)["ResultSet"]["Rows"]
    return [[c.get("VarCharValue") for c in r["Data"]] for r in rows[1:]]


def test_athena_reads_and_writes_an_s3_table():
    try:
        from oblako.services.trino import TrinoService

        if "error" in TrinoService().query("SELECT 1"):
            pytest.skip("Trino not ready")
    except Exception:
        pytest.skip("Trino not available")
    os.environ.setdefault("AWS_ENDPOINT_URL_S3", "http://localhost:9000")

    import boto3

    tables = boto3.client(
        "s3tables",
        endpoint_url=s3tables.start_in_thread(),
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    from oblako.engines.athena import get_client

    athena = get_client()
    bucket = "lake" + uuid.uuid4().hex[:8]
    arn = tables.create_table_bucket(name=bucket)["arn"]
    tables.create_namespace(tableBucketARN=arn, namespace=["web"])
    tables.create_table(
        tableBucketARN=arn,
        namespace="web",
        name="views",
        format="ICEBERG",
        metadata={
            "iceberg": {
                "schema": {
                    "fields": [
                        {"name": "page", "type": "string"},
                        {"name": "views", "type": "long"},
                    ]
                }
            }
        },
    )
    catalog = f"s3tablescatalog/{bucket}"
    try:
        _rows(
            athena,
            "INSERT INTO views VALUES ('/', 120), ('/docs', 45)",
            Catalog=catalog,
            Database="web",
        )
        assert _rows(
            athena,
            "SELECT page, views FROM views ORDER BY views DESC",
            Catalog=catalog,
            Database="web",
        ) == [["/", "120"], ["/docs", "45"]]
        assert _rows(athena, f'SELECT count(*) FROM "{catalog}"."web"."views"') == [
            ["2"]
        ]
    finally:
        with contextlib.suppress(Exception):
            tables.delete_table(tableBucketARN=arn, namespace="web", name="views")
            tables.delete_namespace(tableBucketARN=arn, namespace="web")
            tables.delete_table_bucket(tableBucketARN=arn)
