"""awswrangler and PyIceberg over the local Glue Data Catalog, queried with Athena.

The data-lake flows of a typical S3 chapter, unmodified:
``wr.s3.to_parquet(..., database=, table=)`` registers a partitioned Glue table,
``wr.athena.read_sql_query`` reads it (CTAS + manifest, and plain CSV results),
and PyIceberg's Glue catalog creates and appends an Iceberg table that Athena
reads through the same catalog. Needs S3, the Iceberg REST catalog and Trino.
"""

import os
import uuid

import boto3
import httpx
import pandas as pd
import pyarrow as pa
import pytest

S3 = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
GLUE = "http://localhost:8486"
ATHENA = "http://localhost:8009"


@pytest.fixture(scope="module")
def lake():
    """Point boto3 / awswrangler at oblako; yield a fresh bucket and database."""
    from oblako.services.trino import TrinoService

    try:
        httpx.get("http://localhost:8181/v1/config", timeout=2).raise_for_status()
        if "error" in TrinoService().query("SELECT 1"):
            pytest.skip("Trino not ready")
    except Exception:
        pytest.skip("Trino or the Iceberg REST catalog not available")
    from oblako.engines import athena, glue_catalog

    glue_catalog.start_in_thread()
    athena.start_in_thread()
    env = {
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ENDPOINT_URL_S3": S3,
        "AWS_ENDPOINT_URL_GLUE": GLUE,
        "AWS_ENDPOINT_URL_ATHENA": ATHENA,
        "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
        "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    import awswrangler as wr

    wr.config.s3_endpoint_url = S3
    suffix = uuid.uuid4().hex[:8]
    bucket, database = f"lake-{suffix}", f"lake_{suffix}"
    boto3.client("s3").create_bucket(Bucket=bucket)
    wr.catalog.create_database(database)
    yield wr, bucket, database
    wr.catalog.delete_database(database)
    wr.s3.delete_objects(f"s3://{bucket}/")
    boto3.client("s3").delete_bucket(Bucket=bucket)
    wr.config.s3_endpoint_url = None
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_parquet_table_registered_and_queried(lake):
    wr, bucket, database = lake
    orders = pd.DataFrame(
        {
            "order_id": range(6),
            "aov": [190.0, 200.0, 194.0, 196.0, 180.0, 210.0],
            "order_month": ["2026-01"] * 3 + ["2026-02"] * 3,
        }
    )
    wr.s3.to_parquet(
        orders,
        path=f"s3://{bucket}/orders/",
        dataset=True,
        mode="overwrite",
        database=database,
        table="orders",
        partition_cols=["order_month"],
    )
    partitions = wr.catalog.get_partitions(database=database, table="orders")
    assert sorted(partitions.values()) == [["2026-01"], ["2026-02"]]
    february = wr.catalog.get_partitions(
        database=database, table="orders", expression="order_month = '2026-02'"
    )
    assert list(february.values()) == [["2026-02"]]

    # the default path: CTAS into a temporary Glue table, read via its manifest
    monthly = wr.athena.read_sql_query(
        "SELECT order_month, round(avg(aov), 2) AS aov FROM orders GROUP BY 1",
        database=database,
    ).sort_values("order_month")
    assert monthly.to_dict("records") == [
        {"order_month": "2026-01", "aov": 194.67},
        {"order_month": "2026-02", "aov": 195.33},
    ]
    assert list(wr.catalog.tables(database=database)["Table"]) == ["orders"]

    wr.s3.to_parquet(
        orders.assign(order_month="2026-03"),
        path=f"s3://{bucket}/orders/",
        dataset=True,
        mode="append",
        database=database,
        table="orders",
        partition_cols=["order_month"],
    )
    counted = wr.athena.read_sql_query(
        "SELECT count(*) AS n FROM orders WHERE order_month >= '2026-02'",
        database=database,
        ctas_approach=False,
    )
    assert counted["n"].tolist() == [9]  # 3 in February + 6 appended to March


def test_iceberg_table_through_the_glue_catalog(lake):
    wr, bucket, database = lake
    from pyiceberg.catalog import load_catalog

    catalog = load_catalog(
        "glue",
        **{
            "type": "glue",
            "glue.endpoint": GLUE,
            "glue.region": "us-east-1",
            "s3.endpoint": S3,
            "s3.region": "us-east-1",
            "s3.access-key-id": "test",
            "s3.secret-access-key": "test",
            "warehouse": f"s3://{bucket}/warehouse",
        },
    )
    name = f"{database}.applications"
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "score": [0.2, 0.7, 0.9]})
    table = catalog.create_table(name, schema=rows.schema)
    table.append(rows)
    table.append(rows)  # a second commit: UpdateTable moves the registration
    assert catalog.load_table(name).scan().to_arrow().num_rows == 6

    # Athena reaches it through the same catalog (Trino redirects to Iceberg)
    out = wr.athena.read_sql_query(
        "SELECT count(*) AS n, round(avg(score), 2) AS s FROM applications",
        database=database,
        ctas_approach=False,
    )
    assert out.to_dict("records") == [{"n": 6, "s": 0.6}]
    catalog.drop_table(name)
