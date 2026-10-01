"""Unit tests for the S3 extensions engine's parsing and the :9000 front routing."""

import pytest

from oblako.engines.s3_ext import (
    check_tags,
    parse_tag_header,
    parse_tagset,
    tagging_xml,
)
from oblako.engines.s3_ext import inventory
from oblako.services.s3proxy import _nginx_conf


def test_tag_header_and_xml_round_trip():
    tags = parse_tag_header("stage=raw&owner=data%20eng&empty=")
    assert tags == [
        {"Key": "stage", "Value": "raw"},
        {"Key": "owner", "Value": "data eng"},
        {"Key": "empty", "Value": ""},
    ]
    assert parse_tagset(tagging_xml(tags).encode()) == tags


def test_tagging_xml_escapes_values():
    xml = tagging_xml([{"Key": "a&b", "Value": "<x>"}])
    assert "a&amp;b" in xml and "&lt;x&gt;" in xml
    assert parse_tagset(xml.encode()) == [{"Key": "a&b", "Value": "<x>"}]


@pytest.mark.parametrize(
    ("tags", "problem"),
    [
        ([{"Key": f"k{i}", "Value": "v"} for i in range(11)], "greater than 10"),
        ([{"Key": "a", "Value": "1"}, {"Key": "a", "Value": "2"}], "same key"),
        ([{"Key": "", "Value": "v"}], "TagKey"),
        ([{"Key": "k", "Value": "v" * 257}], "TagValue"),
    ],
)
def test_invalid_tags_are_rejected(tags, problem):
    assert problem in check_tags(tags)


def test_valid_tags_pass():
    assert check_tags([{"Key": "k", "Value": "v"}]) is None


def test_inventory_configuration_is_parsed_and_namespaced():
    body = (
        b"<InventoryConfiguration><Id>daily</Id><IsEnabled>true</IsEnabled>"
        b"<Destination><S3BucketDestination><Bucket>arn:aws:s3:::dest</Bucket>"
        b"<Format>Parquet</Format><Prefix>reports/</Prefix></S3BucketDestination>"
        b"</Destination><Filter><Prefix>raw/</Prefix></Filter>"
        b"<OptionalFields><Field>Size</Field><Field>ETag</Field></OptionalFields>"
        b"</InventoryConfiguration>"
    )
    config = inventory.normalize(body)
    assert (config.id, config.enabled, config.destination) == ("daily", True, "dest")
    assert (config.format, config.prefix, config.filter_prefix) == (
        "Parquet",
        "reports",
        "raw/",
    )
    assert config.optional_fields == ["Size", "ETag"]
    assert 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"' in config.xml


def test_inventory_without_destination_is_rejected():
    with pytest.raises(ValueError, match="Destination"):
        inventory.parse("<InventoryConfiguration><Id>x</Id></InventoryConfiguration>")


def test_front_routes_only_tagging_inventory_and_tagged_writes():
    conf = _nginx_conf(9001, 8020)
    assert "server host.docker.internal:9001" in conf
    assert "server host.docker.internal:8020" in conf
    # query parameter *names*, so prefix=tagging stays on S3Proxy
    assert '"~(^|&)(tagging|inventory)(=|&|$)"' in conf
    assert '"0::" s3proxy;' in conf
    assert "client_max_body_size 0;" in conf
