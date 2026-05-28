"""Unit tests for IcebergCatalogService (pure config; no container required)."""

from oblako.services.iceberg import DEFAULT_WAREHOUSE, IcebergCatalogService


def test_defaults():
    svc = IcebergCatalogService()
    assert svc.host_port == 8181
    assert svc.endpoint_url == "http://localhost:8181"
    assert svc.warehouse == DEFAULT_WAREHOUSE == "s3://oblako-iceberg/"
    assert svc.image == "tabulario/iceberg-rest:latest"


def test_env_wires_warehouse_to_s3proxy():
    svc = IcebergCatalogService()
    env = svc.environment
    assert env["CATALOG_WAREHOUSE"] == "s3://oblako-iceberg/"
    assert env["CATALOG_IO__IMPL"] == "org.apache.iceberg.aws.s3.S3FileIO"
    # S3Proxy lives on the host; the container reaches it via host.docker.internal.
    assert env["CATALOG_S3_ENDPOINT"].endswith(":9000")
    assert env["CATALOG_S3_PATH-STYLE-ACCESS"] == "true"
    assert svc.extra_hosts == {"host.docker.internal": "host-gateway"}
