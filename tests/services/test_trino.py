"""Unit tests for TrinoService (pure config; the live query test needs Docker)."""

from oblako.services.trino import TrinoService


def test_defaults():
    svc = TrinoService()
    assert svc.host_port == 8485  # Trino's internal :8080 mapped here (8080 is heavily used)
    assert svc.endpoint_url == "http://localhost:8485"
    assert svc.image == "trinodb/trino:latest"


def test_athena_alias():
    from oblako.services.platform import Oblako
    o = Oblako()
    assert o.athena is o.trino  # Athena is Trino under the hood on AWS too


def test_iceberg_catalog_properties_written(tmp_path, monkeypatch):
    # The catalog config must point at oblako's Iceberg REST + S3Proxy via host.docker.internal.
    monkeypatch.setattr("oblako.services.trino.TRINO_CATALOG_DIR", tmp_path)
    TrinoService(host_port=9999)
    props = (tmp_path / "iceberg.properties").read_text()
    assert "connector.name=iceberg" in props
    assert "iceberg.rest-catalog.uri=http://host.docker.internal:8181" in props
    assert "s3.endpoint=http://host.docker.internal:9000" in props
    assert "s3.path-style-access=true" in props
