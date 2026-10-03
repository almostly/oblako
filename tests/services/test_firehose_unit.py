"""Unit tests for Firehose object naming, prefix rules and the Redshift COPY."""

import datetime
import importlib

import pytest

from oblako.engines.firehose.app import (
    InvalidArgument,
    _check_prefixes,
    copy_statement,
    evaluate_prefix,
    object_name,
)

WHEN = datetime.datetime(2018, 8, 27, 10, 30, 5, tzinfo=datetime.timezone.utc)


def test_default_time_prefix_is_appended():
    assert evaluate_prefix("", WHEN) == "2018/08/27/10/"
    assert (
        evaluate_prefix("myFirehosePrefix/", WHEN) == "myFirehosePrefix/2018/08/27/10/"
    )


def test_timestamp_and_random_expressions():
    prefix = (
        "myPrefix/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    )
    assert evaluate_prefix(prefix, WHEN) == "myPrefix/year=2018/month=08/day=27/"
    random = evaluate_prefix("p/!{firehose:random-string}/!{timestamp:yyyy}/", WHEN)
    assert len(random.split("/")[1]) == 11
    assert evaluate_prefix("!{timestamp:yyyy'-q'}/", WHEN) == "2018-q/"


def test_error_output_type():
    error = evaluate_prefix(
        "fail/!{firehose:error-output-type}/!{timestamp:yyyy}/",
        WHEN,
        "format-conversion-failed",
    )
    assert error == "fail/format-conversion-failed/2018/"


def test_object_name_follows_firehose():
    key = object_name("logs/", "clicks", 1, WHEN, ".gz")
    assert key.startswith("logs/2018/08/27/10/clicks-1-2018-08-27-10-30-05-")
    assert key.endswith(".gz")


@pytest.mark.parametrize(
    "prefix, error_prefix, destination",
    [
        ("!{timestamp:yyyy}/", None, "s3"),  # expressions need an error prefix
        ("p/", "e/!{timestamp:yyyy}/", "s3"),  # error prefix without error type
        ("p/!{firehose:error-output-type}/", "e/", "s3"),
        ("p/!{timestamp:yyyy}/", "e/!{firehose:error-output-type}/", "redshift"),
        ("p/!{partitionKeyFromQuery:id}/", "e/!{firehose:error-output-type}/", "s3"),
    ],
)
def test_invalid_prefixes_are_rejected(prefix, error_prefix, destination):
    with pytest.raises(InvalidArgument):
        _check_prefixes(prefix, error_prefix, destination)


def test_copy_statement():
    conf = {
        "RoleARN": "arn:aws:iam::123456789012:role/firehose",
        "CopyCommand": {
            "DataTableName": "events",
            "DataTableColumns": "id,kind",
            "CopyOptions": "JSON 'auto' GZIP",
        },
    }
    assert copy_statement(conf, "bucket", "rs/key.gz") == (
        "COPY events (id,kind) FROM 's3://bucket/rs/key.gz' "
        "CREDENTIALS 'aws_iam_role=arn:aws:iam::123456789012:role/firehose' "
        "JSON 'auto' GZIP"
    )


def test_missing_kinesis_source_registers_nothing(tmp_path, monkeypatch):
    """A create that fails on its source stream leaves no stream behind."""
    # the package exports its ASGI app as `app`, so import the module by name
    firehose_app = importlib.import_module("oblako.engines.firehose.app")

    class NoStreams:
        def describe_stream(self, StreamName):
            raise RuntimeError(f"Stream {StreamName} not found")

    monkeypatch.setattr(firehose_app, "_kinesis_client", NoStreams)
    executor = firehose_app.FirehoseExecutor(state_path=tmp_path / "streams.json")
    role = "arn:aws:iam::123456789012:role/firehose"
    with pytest.raises(RuntimeError):
        executor.create_delivery_stream(
            {
                "DeliveryStreamName": "from-nowhere",
                "DeliveryStreamType": "KinesisStreamAsSource",
                "KinesisStreamSourceConfiguration": {
                    "KinesisStreamARN": "arn:aws:kinesis:us-east-1:123456789012:stream/nope",
                    "RoleARN": role,
                },
                "ExtendedS3DestinationConfiguration": {
                    "RoleARN": role,
                    "BucketARN": "arn:aws:s3:::bucket",
                },
            }
        )
    assert executor.list_delivery_streams()["DeliveryStreamNames"] == []
    assert not (tmp_path / "streams.json").exists()
