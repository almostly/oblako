"""Unit tests for the S3 extensions engine's parsing and the :9000 front routing."""

import json

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
    assert "server @HOST@:9001" in conf
    assert "server @HOST@:8020" in conf
    # query parameter *names*, so prefix=tagging stays on S3Proxy
    assert '"~(^|&)(tagging|inventory|policy|policyStatus|notification)(=|&|$)"' in conf
    # everything else goes straight to S3Proxy
    assert '"0:0:0::" s3proxy;' in conf
    # CreateBucket (PUT on a bucket, no key or query) goes to the engine
    assert '"~^PUT:/[^/?]+/?$" 1;' in conf
    assert "client_max_body_size 0;" in conf


# -----------------------------------------------------------------------------
# Bucket policy and event notifications
# -----------------------------------------------------------------------------
def test_policy_public_when_anyone_is_allowed_unconditionally():
    from oblako.engines.s3_ext import is_public

    stmt = {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject"}
    assert is_public(json.dumps({"Statement": [stmt]}))
    assert is_public(json.dumps({"Statement": [{**stmt, "Principal": {"AWS": "*"}}]}))
    assert not is_public(
        json.dumps({"Statement": [{**stmt, "Condition": {"IpAddress": {}}}]})
    )
    assert not is_public(json.dumps({"Statement": [{**stmt, "Effect": "Deny"}]}))


def test_notification_accepts_the_wire_lambda_element_and_assigns_ids():
    from oblako.engines.s3_ext import notifications

    body = (
        b'<NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        b"<CloudFunctionConfiguration><CloudFunction>arn:aws:lambda:us-east-1:1:"
        b"function:f</CloudFunction><Event>s3:ObjectCreated:*</Event><Filter><S3Key>"
        b"<FilterRule><Name>Prefix</Name><Value>raw/</Value></FilterRule>"
        b"<FilterRule><Name>Suffix</Name><Value>.csv</Value></FilterRule></S3Key>"
        b"</Filter></CloudFunctionConfiguration>"
        b"<QueueConfiguration><Id>q</Id><Queue>arn:aws:sqs:us-east-1:1:q</Queue>"
        b"<Event>s3:ObjectRemoved:Delete</Event></QueueConfiguration>"
        b"</NotificationConfiguration>"
    )
    xml = notifications.normalize(body)
    targets, eventbridge = notifications.parse(xml)
    assert not eventbridge
    lam, queue = targets
    assert (lam.kind, lam.prefix, lam.suffix) == ("lambda", "raw/", ".csv")
    assert lam.id  # S3 assigns an Id when none is given
    assert lam.matches("s3:ObjectCreated:Put", "raw/a.csv")
    assert not lam.matches("s3:ObjectCreated:Put", "raw/a.txt")
    assert not lam.matches("s3:ObjectRemoved:Delete", "raw/a.csv")
    assert queue.matches("s3:ObjectRemoved:Delete", "any")


def test_notification_needs_a_destination_arn():
    from oblako.engines.s3_ext import notifications

    with pytest.raises(ValueError, match="Queue"):
        notifications.normalize(
            b"<NotificationConfiguration><QueueConfiguration><Event>s3:ObjectCreated:*"
            b"</Event></QueueConfiguration></NotificationConfiguration>"
        )


@pytest.mark.parametrize(
    ("method", "query", "headers", "event"),
    [
        ("PUT", {}, {}, "s3:ObjectCreated:Put"),
        ("PUT", {}, {"x-amz-copy-source": "/b/k"}, "s3:ObjectCreated:Copy"),
        ("PUT", {"partNumber": "1", "uploadId": "u"}, {}, None),
        ("POST", {"uploadId": "u"}, {}, "s3:ObjectCreated:CompleteMultipartUpload"),
        ("POST", {"uploads": ""}, {}, None),
        ("DELETE", {}, {}, "s3:ObjectRemoved:Delete"),
        ("DELETE", {"uploadId": "u"}, {}, None),
        ("PUT", {"tagging": ""}, {}, None),
    ],
)
def test_event_names(method, query, headers, event):
    from oblako.engines.s3_ext import notifications

    assert notifications.event_name(method, query, headers) == event


def test_event_record_shape():
    from oblako.engines.s3_ext import notifications

    rec = notifications.record(
        "s3:ObjectCreated:Put",
        "lake",
        "raw/my file.csv",
        {"content-length": "15", "etag": '"abc"'},
        "cfg",
    )
    assert rec["eventSource"] == "aws:s3" and rec["eventName"] == "ObjectCreated:Put"
    assert rec["s3"]["bucket"] == {
        "name": "lake",
        "ownerIdentity": {"principalId": "oblako"},
        "arn": "arn:aws:s3:::lake",
    }
    assert rec["s3"]["object"]["key"] == "raw/my+file.csv"  # form-encoded, as S3
    assert (rec["s3"]["object"]["size"], rec["s3"]["object"]["eTag"]) == (15, "abc")
    assert rec["s3"]["configurationId"] == "cfg"


def test_front_logs_writes_and_strips_the_session_token():
    conf = _nginx_conf(9001, 8020)
    assert "access_log /var/log/oblako/writes.log s3_writes if=$s3_write;" in conf
    assert '"status":"$status"' in conf and '"etag":"$upstream_http_etag"' in conf
    assert 'proxy_set_header X-Amz-Security-Token "";' in conf
    assert 'proxy_set_header X-Amz-Optional-Object-Attributes "";' in conf
    assert "policy|policyStatus|notification" in conf


def test_follow_log_fires_completed_writes_in_order(tmp_path, monkeypatch):
    import threading
    import time as _time

    from oblako.engines.s3_ext import notifications

    seen = []
    monkeypatch.setattr(
        notifications, "handle_logged", lambda store, backend, entry: seen.append(entry)
    )
    log = tmp_path / "writes.log"
    log.write_text('{"uri": "/old/before-start"}\n')  # not replayed
    threading.Thread(
        target=notifications.follow_log, args=(None, "", log), daemon=True
    ).start()
    _time.sleep(0.3)
    with open(log, "a") as fh:
        fh.write('{"method":"PUT","uri":"/b/k","status":"200"}\n')
        fh.write('{"method":"DELETE","uri":"/b/k","status":"204"}\n')
        fh.write('{"method":"PUT","uri":"/b/partial"')  # no newline yet
    for _ in range(30):
        if len(seen) >= 2:
            break
        _time.sleep(0.1)
    assert [(e["method"], e["uri"]) for e in seen] == [
        ("PUT", "/b/k"),
        ("DELETE", "/b/k"),
    ]
