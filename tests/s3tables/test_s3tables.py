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
        return (
            httpx.get(f"{s3tables._iceberg_url()}/v1/config", timeout=3).status_code
            < 400
        )
    except Exception:
        return False


# --- unit: ARN + schema helpers (no catalog) -------------------------------
def test_bucket_of_arn():
    arn = s3tables._bucket_arn("lake")
    assert arn == "arn:aws:s3tables:us-east-1:123456789012:bucket/lake"
    assert s3tables._bucket_of(arn) == "lake"
    assert s3tables._bucket_of("lake") == "lake"  # bare name passes through


def test_iceberg_schema_assigns_ids():
    schema = s3tables._iceberg_schema(
        {
            "iceberg": {
                "schema": {
                    "fields": [
                        {"name": "id", "type": "long", "required": True},
                        {"name": "amount", "type": "double"},
                    ]
                }
            }
        }
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
# Marked `integration` so CI's integration job (which starts the catalog) runs
# them; they still skip when the catalog isn't up.
_skip_without_catalog = pytest.mark.skipif(
    not _catalog_up(), reason="Iceberg REST catalog not running"
)


def pytestmark_integration(test):
    return pytest.mark.integration(_skip_without_catalog(test))


@pytest.fixture
def warehouse():
    """Ensure the catalog's warehouse S3 bucket exists (as IcebergCatalogService does)."""
    s3 = boto3.client(
        "s3",
        endpoint_url="http://localhost:9000",
        aws_access_key_id="oblako",
        aws_secret_access_key="oblako",
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        ),
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
        "s3tables",
        endpoint_url=url,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytestmark_integration
def test_bucket_namespace_table_and_metadata_location(client):
    bucket = "lake" + uuid.uuid4().hex[:8]
    arn = client.create_table_bucket(name=bucket)["arn"]
    assert arn.endswith(f":bucket/{bucket}")
    assert bucket in [b["name"] for b in client.list_table_buckets()["tableBuckets"]]

    client.create_namespace(tableBucketARN=arn, namespace=["sales"])
    assert [
        n["namespace"] for n in client.list_namespaces(tableBucketARN=arn)["namespaces"]
    ] == [["sales"]]

    ct = client.create_table(
        tableBucketARN=arn,
        namespace="sales",
        name="orders",
        format="ICEBERG",
        metadata={
            "iceberg": {
                "schema": {
                    "fields": [
                        {"name": "id", "type": "long", "required": True},
                        {"name": "amount", "type": "double"},
                    ]
                }
            }
        },
    )
    assert ct["tableARN"].endswith("/table/sales/orders")
    assert ct["versionToken"]

    tables = client.list_tables(tableBucketARN=arn)["tables"]
    assert [(t["namespace"], t["name"]) for t in tables] == [(["sales"], "orders")]

    ml = client.get_table_metadata_location(
        tableBucketARN=arn, namespace="sales", name="orders"
    )
    # a real Iceberg metadata.json pointer under the warehouse
    assert ml["metadataLocation"].startswith(
        f"s3://{WAREHOUSE_BUCKET}/{bucket}/sales/orders/metadata/"
    )
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
        client.get_table_metadata_location(
            tableBucketARN=arn, namespace="ns", name="nope"
        )
    assert "not" in str(excinfo.value).lower()


# --- the S3 Tables Iceberg REST endpoint (/iceberg) ------------------------
def test_iceberg_namespace_translation():
    from oblako.engines.s3tables import iceberg

    assert iceberg._ns_in("lake", "web") == "lake%1Fweb"
    assert iceberg._body_in("lake", {"namespace": ["web"], "x": 1}) == {
        "namespace": ["lake", "web"],
        "x": 1,
    }
    out = iceberg._body_out(
        "lake",
        {
            "namespaces": [["lake", "web"], ["lake", "app"]],
            "identifiers": [{"namespace": ["lake", "web"], "name": "t"}],
        },
    )
    assert out == {
        "namespaces": [["web"], ["app"]],
        "identifiers": [{"namespace": ["web"], "name": "t"}],
    }


def test_iceberg_config_returns_arn_prefix():
    from starlette.testclient import TestClient

    arn = s3tables._bucket_arn("lake")
    resp = TestClient(s3tables.create_app()).get(
        "/iceberg/v1/config", params={"warehouse": arn}
    )
    assert resp.status_code == 200
    overrides = resp.json()["overrides"]
    assert (
        overrides["prefix"]
        == "arn%3Aaws%3As3tables%3Aus-east-1%3A123456789012%3Abucket%2Flake"
    )
    assert overrides["s3.endpoint"].endswith(":9000")


def test_iceberg_proxy_rewrites_namespaces(monkeypatch):
    from starlette.testclient import TestClient

    from oblako.engines.s3tables import iceberg

    sent = {}

    async def fake_send(method, path, params, content):
        sent.update(method=method, path=path, params=params, content=content)
        body = b'{"namespaces": [["lake", "web"]]}'
        return httpx.Response(
            200, content=body, headers={"content-type": "application/json"}
        )

    monkeypatch.setattr(iceberg, "_send", fake_send)
    prefix = "arn%3Aaws%3As3tables%3Aus-east-1%3A123456789012%3Abucket%2Flake"
    client = TestClient(s3tables.create_app())

    resp = client.get(f"/iceberg/v1/{prefix}/namespaces")
    assert sent["path"] == "/v1/namespaces" and sent["params"]["parent"] == "lake"
    assert resp.json() == {"namespaces": [["web"]]}

    client.get(f"/iceberg/v1/{prefix}/namespaces/web/tables/page_views")
    assert sent["path"] == "/v1/namespaces/lake%1Fweb/tables/page_views"


@pytestmark_integration
def test_pyiceberg_through_the_s3tables_endpoint(client, monkeypatch):
    """PyIceberg configured as for AWS S3 Tables (warehouse = bucket ARN, SigV4)."""
    pa = pytest.importorskip("pyarrow")
    catalog_mod = pytest.importorskip("pyiceberg.catalog")
    for key, value in {
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(key, value)

    bucket = "lake" + uuid.uuid4().hex[:8]
    arn = client.create_table_bucket(name=bucket)["arn"]
    client.create_namespace(tableBucketARN=arn, namespace=["web"])
    catalog = catalog_mod.load_catalog(
        "s3tables",
        **{
            "type": "rest",
            "uri": f"{s3tables.start_in_thread()}/iceberg",
            "warehouse": arn,
            "rest.sigv4-enabled": "true",
            "rest.signing-name": "s3tables",
            "rest.signing-region": "us-east-1",
        },
    )
    views = pa.table({"page": ["/", "/docs"], "views": [120, 45]})
    table = catalog.create_table("web.page_views", schema=views.schema)
    table.append(views)
    try:
        assert catalog.list_tables("web") == [("web", "page_views")]
        assert catalog.load_table("web.page_views").scan().to_arrow().num_rows == 2
        # the same table, seen through the s3tables API
        names = [t["name"] for t in client.list_tables(tableBucketARN=arn)["tables"]]
        assert names == ["page_views"]
    finally:
        catalog.drop_table("web.page_views")
        client.delete_namespace(tableBucketARN=arn, namespace="web")


@pytestmark_integration
def test_table_buckets_live_in_the_catalog(client):
    """Buckets are listed from the catalog (so a restart keeps them), and only marked ones."""
    bucket = "lake" + uuid.uuid4().hex[:8]
    other = "gluedb" + uuid.uuid4().hex[:8]
    client.create_table_bucket(name=bucket)
    # a top-level namespace that is not a table bucket, as a Glue database is
    httpx.post(f"{s3tables._iceberg_url()}/v1/namespaces", json={"namespace": [other]})
    try:
        names = [b["name"] for b in client.list_table_buckets()["tableBuckets"]]
        assert bucket in names and other not in names
        # nothing in the engine's memory: a fresh lookup reads the catalog
        assert (
            client.get_table_bucket(tableBucketARN=s3tables._bucket_arn(bucket))["name"]
            == bucket
        )
    finally:
        client.delete_table_bucket(tableBucketARN=s3tables._bucket_arn(bucket))
        httpx.delete(f"{s3tables._iceberg_url()}/v1/namespaces/{other}")
    names = [b["name"] for b in client.list_table_buckets()["tableBuckets"]]
    assert bucket not in names
