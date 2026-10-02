"""Integration test: Kinesis Data Firehose delivers records to S3.

Requires S3Proxy (no Docker for the engine itself); the Kinesis-source test needs
moto, the Redshift one oblako's Redshift, the Parquet one pyarrow and the Glue
engine. Unmodified boto3 ``firehose``. Override OBLAKO_TEST_S3_ENDPOINT /
OBLAKO_TEST_RS_PORT to point at isolated services.
"""

import json
import os
import re
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "firehose-ci"
ROLE = "arn:aws:iam::123456789012:role/firehose"
RS_PORT = int(os.environ.get("OBLAKO_TEST_RS_PORT", "5439"))


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        ),
    )


def _empty(s3, prefix):
    """Remove what earlier runs left under a prefix."""
    for obj in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []):
        s3.delete_object(Bucket=BUCKET, Key=obj["Key"])


def test_firehose_delivers_records_to_s3():
    try:
        _s3().list_buckets()
    except Exception:
        pytest.skip("S3Proxy not available")

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.engines.firehose import get_client

    s3 = _s3()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass

    _empty(s3, "events/")
    fh = get_client()
    stream = "events"
    fh.create_delivery_stream(
        DeliveryStreamName=stream,
        DeliveryStreamType="DirectPut",
        ExtendedS3DestinationConfiguration={
            "RoleARN": ROLE,
            "BucketARN": f"arn:aws:s3:::{BUCKET}",
            "Prefix": "events/",
            "BufferingHints": {"IntervalInSeconds": 1, "SizeInMBs": 5},
            "CompressionFormat": "UNCOMPRESSED",
        },
    )
    try:
        desc = fh.describe_delivery_stream(DeliveryStreamName=stream)[
            "DeliveryStreamDescription"
        ]
        assert desc["DeliveryStreamStatus"] == "ACTIVE"
        assert stream in fh.list_delivery_streams()["DeliveryStreamNames"]

        fh.put_record(
            DeliveryStreamName=stream, Record={"Data": b'{"id":1,"amt":10}\n'}
        )
        fh.put_record_batch(
            DeliveryStreamName=stream,
            Records=[
                {"Data": b'{"id":2,"amt":20}\n'},
                {"Data": b'{"id":3,"amt":30}\n'},
            ],
        )

        # the delivery loop stages the buffered records to S3 within the interval
        objects = []
        for _ in range(20):
            objects = s3.list_objects_v2(Bucket=BUCKET, Prefix="events/").get(
                "Contents", []
            )
            if objects:
                break
            time.sleep(0.5)
        assert objects, "firehose did not stage any object to S3"

        records = []
        for obj in objects:
            body = s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read().decode()
            records += [json.loads(line) for line in body.splitlines() if line.strip()]
        assert {1, 2, 3} <= {r["id"] for r in records}
        assert {10, 20, 30} <= {r["amt"] for r in records}

        # <prefix>yyyy/MM/dd/HH/<name>-<version>-yyyy-MM-dd-HH-mm-ss-<uuid>
        assert re.fullmatch(
            r"events/\d{4}/\d{2}/\d{2}/\d{2}/events-1-"
            r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-[0-9a-f-]{36}",
            objects[0]["Key"],
        ), objects[0]["Key"]
    finally:
        fh.delete_delivery_stream(DeliveryStreamName=stream)


def test_firehose_kinesis_stream_as_source():
    try:
        _s3().list_buckets()
        import docker

        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker or S3Proxy not available")

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.engines.firehose import get_client
    from oblako.services import MotoService

    # the source Kinesis stream is served by moto (fast + reliable); the firehose
    # consumer reads it via AWS_ENDPOINT_URL_KINESIS
    try:
        moto_endpoint = MotoService().endpoint_url
        boto3.client(
            "kinesis",
            endpoint_url=moto_endpoint,
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        ).list_streams()
    except Exception as err:
        pytest.skip(f"moto Kinesis unavailable: {err}")
    os.environ["AWS_ENDPOINT_URL_KINESIS"] = moto_endpoint
    kinesis = boto3.client(
        "kinesis",
        endpoint_url=moto_endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )

    s3 = _s3()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass

    _empty(s3, "k/")
    source = "txns"
    try:
        kinesis.delete_stream(StreamName=source)
    except Exception:
        pass
    kinesis.create_stream(StreamName=source, ShardCount=1)
    # put before the delivery stream exists: Firehose starts at LATEST, so skipped
    kinesis.put_record(StreamName=source, Data=b'{"id": -1}\n', PartitionKey="early")
    source_arn = kinesis.describe_stream(StreamName=source)["StreamDescription"][
        "StreamARN"
    ]

    fh = get_client()
    fh.create_delivery_stream(
        DeliveryStreamName="from-kinesis",
        DeliveryStreamType="KinesisStreamAsSource",
        KinesisStreamSourceConfiguration={
            "KinesisStreamARN": source_arn,
            "RoleARN": ROLE,
        },
        ExtendedS3DestinationConfiguration={
            "RoleARN": ROLE,
            "BucketARN": f"arn:aws:s3:::{BUCKET}",
            "Prefix": "k/",
            "BufferingHints": {"IntervalInSeconds": 1, "SizeInMBs": 5},
        },
    )
    for i in range(3):
        kinesis.put_record(
            StreamName=source,
            Data=(json.dumps({"id": i}) + "\n").encode(),
            PartitionKey=str(i),
        )
    try:
        objects = []
        for _ in range(30):
            objects = s3.list_objects_v2(Bucket=BUCKET, Prefix="k/").get("Contents", [])
            if objects:
                break
            time.sleep(0.5)
        assert objects, "firehose did not deliver Kinesis-sourced records to S3"
        ids = set()
        for obj in objects:
            body = s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read().decode()
            ids |= {json.loads(x)["id"] for x in body.splitlines() if x.strip()}
        assert ids == {0, 1, 2}

        # PutRecord is rejected for a Kinesis-source stream
        with pytest.raises(fh.exceptions.ClientError):
            fh.put_record(DeliveryStreamName="from-kinesis", Record={"Data": b"x"})
    finally:
        fh.delete_delivery_stream(DeliveryStreamName="from-kinesis")
        kinesis.delete_stream(StreamName=source)


def test_firehose_redshift_destination():
    """Firehose stages each batch to S3 and loads it with its COPY, CopyOptions included."""
    try:
        _s3().list_buckets()
    except Exception:
        pytest.skip("S3Proxy not available")
    import psycopg

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.engines.firehose import get_client

    dsn = f"postgresql://oblako:oblako@localhost:{RS_PORT}/oblako"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS fh_txns")
            conn.execute("CREATE TABLE fh_txns (id int, amt numeric(10, 2))")
    except psycopg.OperationalError:
        pytest.skip("oblako Redshift not available")

    s3 = _s3()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass

    _empty(s3, "rs/")
    fh = get_client()
    fh.create_delivery_stream(
        DeliveryStreamName="to-redshift",
        DeliveryStreamType="DirectPut",
        RedshiftDestinationConfiguration={
            "RoleARN": ROLE,
            "ClusterJDBCURL": f"jdbc:redshift://localhost:{RS_PORT}/oblako",
            "Username": "oblako",
            "Password": "oblako",
            "CopyCommand": {
                "DataTableName": "fh_txns",
                "DataTableColumns": "id,amt",
                "CopyOptions": "JSON 'auto' GZIP",
            },
            "S3Configuration": {
                "RoleARN": ROLE,
                "BucketARN": f"arn:aws:s3:::{BUCKET}",
                "Prefix": "rs/",
                "CompressionFormat": "GZIP",
                "BufferingHints": {"IntervalInSeconds": 1, "SizeInMBs": 5},
            },
        },
    )
    try:
        for rid, amt in ((1, 10), (2, 20), (3, 30)):
            fh.put_record(
                DeliveryStreamName="to-redshift",
                Record={"Data": json.dumps({"id": rid, "amt": amt}).encode()},
            )

        rows = []
        for _ in range(40):
            with psycopg.connect(dsn) as conn:
                rows = conn.execute(
                    "SELECT id, amt FROM fh_txns ORDER BY id"
                ).fetchall()
            if len(rows) == 3:
                break
            time.sleep(0.5)
        assert [(r[0], int(r[1])) for r in rows] == [(1, 10), (2, 20), (3, 30)]

        # the batch was staged to S3, gzipped, before the COPY
        staged = s3.list_objects_v2(Bucket=BUCKET, Prefix="rs/").get("Contents", [])
        assert staged and staged[0]["Key"].endswith(".gz")
    finally:
        fh.delete_delivery_stream(DeliveryStreamName="to-redshift")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS fh_txns")


def test_firehose_converts_json_to_parquet():
    """Record format conversion: JSON to Parquet with a Glue table's schema."""
    try:
        _s3().list_buckets()
    except Exception:
        pytest.skip("S3Proxy not available")
    pa = pytest.importorskip("pyarrow")
    import io

    import pyarrow.parquet as pq

    from oblako.engines import glue_catalog

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    os.environ["AWS_ENDPOINT_URL_GLUE"] = glue_catalog.start_in_thread()
    from oblako.engines.firehose import get_client

    glue = boto3.client(
        "glue",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    try:
        glue.create_database(DatabaseInput={"Name": "fh_db"})
    except glue.exceptions.AlreadyExistsException:
        pass
    try:
        glue.delete_table(DatabaseName="fh_db", Name="orders")
    except glue.exceptions.EntityNotFoundException:
        pass
    glue.create_table(
        DatabaseName="fh_db",
        TableInput={
            "Name": "orders",
            "StorageDescriptor": {
                "Columns": [
                    {"Name": "id", "Type": "bigint"},
                    {"Name": "total", "Type": "decimal(10,2)"},
                    {"Name": "placed_at", "Type": "timestamp"},
                    {"Name": "note", "Type": "string"},
                ],
                "Location": f"s3://{BUCKET}/orders/",
            },
            "TableType": "EXTERNAL_TABLE",
        },
    )
    s3 = _s3()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass

    _empty(s3, "orders/")
    _empty(s3, "errors/")
    fh = get_client()
    fh.create_delivery_stream(
        DeliveryStreamName="to-parquet",
        DeliveryStreamType="DirectPut",
        ExtendedS3DestinationConfiguration={
            "RoleARN": ROLE,
            "BucketARN": f"arn:aws:s3:::{BUCKET}",
            "Prefix": "orders/dt=!{timestamp:yyyy-MM-dd}/",
            "ErrorOutputPrefix": "errors/!{firehose:error-output-type}/",
            "BufferingHints": {"IntervalInSeconds": 1, "SizeInMBs": 64},
            "DataFormatConversionConfiguration": {
                "Enabled": True,
                "SchemaConfiguration": {
                    "DatabaseName": "fh_db",
                    "TableName": "orders",
                    "RoleARN": ROLE,
                },
                "InputFormatConfiguration": {"Deserializer": {"OpenXJsonSerDe": {}}},
                "OutputFormatConfiguration": {"Serializer": {"ParquetSerDe": {}}},
            },
        },
    )
    try:
        records = [
            {"ID": 1, "total": "12.50", "placed_at": "2026-10-01T09:00:00Z"},
            {"id": 2, "total": 7, "placed_at": 1790000000000, "note": "gift"},
            {"id": "not a number", "total": 1, "placed_at": "2026-10-01T09:00:00Z"},
        ]
        fh.put_record_batch(
            DeliveryStreamName="to-parquet",
            Records=[{"Data": json.dumps(r).encode()} for r in records],
        )
        objects = []
        for _ in range(30):
            objects = s3.list_objects_v2(Bucket=BUCKET, Prefix="orders/dt=").get(
                "Contents", []
            )
            if objects:
                break
            time.sleep(0.5)
        assert objects and objects[0]["Key"].endswith(".parquet")
        body = s3.get_object(Bucket=BUCKET, Key=objects[0]["Key"])["Body"].read()
        table = pq.read_table(io.BytesIO(body))
        assert table.schema.field("id").type == pa.int64()
        assert table.schema.field("total").type == pa.decimal128(10, 2)
        assert table.column("id").to_pylist() == [1, 2]  # keys match case-insensitively
        assert table.column("note").to_pylist() == [None, "gift"]

        errors = s3.list_objects_v2(
            Bucket=BUCKET, Prefix="errors/format-conversion-failed/"
        ).get("Contents", [])
        assert errors
        failed = s3.get_object(Bucket=BUCKET, Key=errors[0]["Key"])["Body"].read()
        assert (
            json.loads(failed)["lastErrorCode"] == "DataFormatConversion.MalformedData"
        )
    finally:
        fh.delete_delivery_stream(DeliveryStreamName="to-parquet")
        for prefix in ("orders/", "errors/"):
            for obj in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get(
                "Contents", []
            ):
                s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
