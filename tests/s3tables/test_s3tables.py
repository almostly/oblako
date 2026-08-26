"""Tests for the local S3 Tables (`s3tables`) engine.

The engine serves the real `s3tables` rest-json protocol in-process and maps its
control plane onto oblako's Iceberg REST catalog. The round-trip tests need that
catalog (and S3Proxy for the warehouse), so they skip when it isn't running:

    docker compose up -d iceberg s3proxy

The path-parsing / ARN unit tests run anywhere.
"""

import uuid

import boto3
import httpx
import pytest
from botocore.config import Config

from oblako.engines import s3tables

WAREHOUSE_BUCKET = "oblako-iceberg"


def _catalog_up() -> bool:
    try:
        return httpx.get(f"{s3tables._iceberg_url()}/v1/config", timeout=3).status_code < 400
    except Exception:
        return False


# --- unit: ARN + schema helpers (no catalog) -------------------------------
def test_bucket_of_arn():
    arn = s3tables._bucket_arn("lake")
    assert arn == "arn:aws:s3tables:us-east-1:000000000000:bucket/lake"
    assert s3tables._bucket_of(arn) == "lake"
    assert s3tables._bucket_of("lake") == "lake"  # bare name passes through


def test_iceberg_schema_assigns_ids():
    schema = s3tables._iceberg_schema(
        {"iceberg": {"schema": {"fields": [
            {"name": "id", "type": "long", "required": True},
            {"name": "amount", "type": "double"},
        ]}}}
    )
    assert schema["type"] == "struct"
    assert [(f["id"], f["name"], f["required"]) for f in schema["fields"]] == [
        (1, "id", True),
        (2, "amount", False),
    ]


def test_two_level_namespace_path_is_unit_separated():
    # bucket + namespace join with the Iceberg unit separator, percent-encoded
    assert s3tables._ns_path("lake", "sales") == "lake%1Fsales"


def test_notebook_env_exposes_s3tables():
    from oblako.notebook import ENDPOINTS

    assert "AWS_ENDPOINT_URL_S3TABLES" in ENDPOINTS


# --- integration: full control-plane round-trip over the Iceberg catalog ----
pytestmark_integration = pytest.mark.skipif(
    not _catalog_up(), reason="Iceberg REST catalog not running"
)


@pytest.fixture
def warehouse():
    """Ensure the catalog's warehouse S3 bucket exists (as IcebergCatalogService does)."""
    s3 = boto3.client(
        "s3", endpoint_url="http://localhost:9000",
        aws_access_key_id="oblako", aws_secret_access_key="oblako", region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required"),
    )
    try:
        s3.create_bucket(Bucket=WAREHOUSE_BUCKET)
    except Exception:
        pass
    return s3


@pytest.fixture
def client(warehouse):
    url = s3tables.start_in_thread()
    return boto3.client(
        "s3tables", endpoint_url=url, region_name="us-east-1",
        aws_access_key_id="test", aws_secret_access_key="test",
    )


@pytestmark_integration
def test_bucket_namespace_table_and_metadata_location(client):
    bucket = "lake" + uuid.uuid4().hex[:8]
    arn = client.create_table_bucket(name=bucket)["arn"]
    assert arn.endswith(f":bucket/{bucket}")
    assert bucket in [b["name"] for b in client.list_table_buckets()["tableBuckets"]]

    client.create_namespace(tableBucketARN=arn, namespace=["sales"])
    assert [n["namespace"] for n in client.list_namespaces(tableBucketARN=arn)["namespaces"]] == [["sales"]]

    ct = client.create_table(
        tableBucketARN=arn, namespace="sales", name="orders", format="ICEBERG",
        metadata={"iceberg": {"schema": {"fields": [
            {"name": "id", "type": "long", "required": True},
            {"name": "amount", "type": "double"},
        ]}}},
    )
    assert ct["tableARN"].endswith("/table/sales/orders")
    assert ct["versionToken"]

    tables = client.list_tables(tableBucketARN=arn)["tables"]
    assert [(t["namespace"], t["name"]) for t in tables] == [(["sales"], "orders")]

    ml = client.get_table_metadata_location(tableBucketARN=arn, namespace="sales", name="orders")
    # a real Iceberg metadata.json pointer under the warehouse
    assert ml["metadataLocation"].startswith(f"s3://{WAREHOUSE_BUCKET}/{bucket}/sales/orders/metadata/")
    assert ml["metadataLocation"].endswith(".metadata.json")

    gt = client.get_table(tableBucketARN=arn, namespace="sales", name="orders")
    assert gt["format"] == "ICEBERG" and gt["namespace"] == ["sales"]

    # cleanup
    client.delete_table(tableBucketARN=arn, namespace="sales", name="orders")
    client.delete_namespace(tableBucketARN=arn, namespace="sales")


@pytestmark_integration
def test_get_table_metadata_location_missing_table(client):
    bucket = "lake" + uuid.uuid4().hex[:8]
    arn = client.create_table_bucket(name=bucket)["arn"]
    client.create_namespace(tableBucketARN=arn, namespace=["ns"])
    with pytest.raises(Exception) as excinfo:  # botocore ClientError (NotFound)
        client.get_table_metadata_location(tableBucketARN=arn, namespace="ns", name="nope")
    assert "not" in str(excinfo.value).lower()
