"""Unit tests for the Glue Data Catalog shim (boto3 ``glue`` over Iceberg REST)."""

from oblako.engines.glue_catalog import _ACTIONS, _iceberg_to_glue_type, _table_to_glue
from oblako.services.glue_catalog import GlueCatalogService


def test_actions_registered():
    # The bare minimum surface boto3 code typically hits.
    expected = {
        "AWSGlue.GetDatabases", "AWSGlue.GetDatabase", "AWSGlue.CreateDatabase",
        "AWSGlue.GetTables", "AWSGlue.GetTable",
    }
    assert expected.issubset(_ACTIONS.keys())


def test_iceberg_to_glue_type():
    assert _iceberg_to_glue_type("long") == "bigint"
    assert _iceberg_to_glue_type("string") == "string"
    assert _iceberg_to_glue_type("double") == "double"
    assert _iceberg_to_glue_type("timestamptz") == "timestamp"
    # Nested types (struct/list/map) coarse-fallback to string.
    assert _iceberg_to_glue_type({"type": "struct", "fields": []}) == "string"
    # Unknown -> string.
    assert _iceberg_to_glue_type("unknown_type") == "string"


def test_table_to_glue_shape():
    iceberg_json = {
        "metadata-location": "s3://oblako-iceberg/credit/applicants/metadata/00001.json",
        "metadata": {"schemas": [{"fields": [
            {"id": 1, "name": "id", "type": "long"},
            {"id": 2, "name": "score", "type": "double"},
            {"id": 3, "name": "status", "type": "string"},
        ]}]},
    }
    out = _table_to_glue("credit", "applicants", iceberg_json)
    assert out["Name"] == "applicants" and out["DatabaseName"] == "credit"
    assert out["TableType"] == "EXTERNAL_TABLE"
    assert out["Parameters"]["table_type"] == "ICEBERG"
    cols = out["StorageDescriptor"]["Columns"]
    assert cols == [{"Name": "id", "Type": "bigint"},
                    {"Name": "score", "Type": "double"},
                    {"Name": "status", "Type": "string"}]
    assert "s3://oblako-iceberg/credit/applicants" in out["StorageDescriptor"]["Location"]


def test_service_endpoint():
    svc = GlueCatalogService()
    assert svc.endpoint_url == "http://localhost:8486"
