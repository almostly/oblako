"""Integration test: Kinesis Data Firehose delivers records to S3.

Requires S3Proxy (no Docker for the engine itself). A DirectPut delivery stream
with an S3 destination buffers PutRecord/PutRecordBatch data and stages it as S3
objects on the buffering interval — unmodified boto3 ``firehose``. Override
OBLAKO_TEST_S3_ENDPOINT to point at an isolated S3Proxy.
"""

import json
import os
import time

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "firehose-ci"
ROLE = "arn:aws:iam::000000000000:role/firehose"


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

        # the key follows the Firehose prefix/YYYY/MM/DD/HH/name-ts-uuid layout
        assert objects[0]["Key"].startswith("events/")
        assert f"/{stream}-" in objects[0]["Key"]
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

    source = "txns"
    try:
        kinesis.delete_stream(StreamName=source)
    except Exception:
        pass
    kinesis.create_stream(StreamName=source, ShardCount=1)
    for i in range(3):
        kinesis.put_record(
            StreamName=source,
            Data=(json.dumps({"id": i}) + "\n").encode(),
            PartitionKey=str(i),
        )
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
    try:
        _s3().list_buckets()
        import docker

        docker_client = docker.from_env()
        docker_client.ping()
    except Exception:
        pytest.skip("Docker or S3Proxy not available")
    import psycopg

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    from oblako.engines.firehose import get_client

    dsn = "postgresql://postgres:firehosepw@localhost:5433/warehouse"
    docker_client.containers.run(
        "postgres:16-alpine",
        detach=True,
        remove=True,
        name="oblako-fh-pg",
        environment={"POSTGRES_PASSWORD": "firehosepw", "POSTGRES_DB": "warehouse"},
        ports={"5432/tcp": 5433},
    )
    try:
        for _ in range(60):
            try:
                with psycopg.connect(dsn) as conn:
                    conn.execute("SELECT 1")
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("postgres did not become ready")

        with psycopg.connect(dsn) as conn:
            conn.execute("CREATE TABLE txns (id int, amt numeric)")

        s3 = _s3()
        try:
            s3.create_bucket(Bucket=BUCKET)
        except s3.exceptions.ClientError:
            pass

        fh = get_client()
        fh.create_delivery_stream(
            DeliveryStreamName="to-redshift",
            DeliveryStreamType="DirectPut",
            RedshiftDestinationConfiguration={
                "RoleARN": ROLE,
                "ClusterJDBCURL": "jdbc:redshift://localhost:5433/warehouse",
                "Username": "postgres",
                "Password": "firehosepw",
                "CopyCommand": {"DataTableName": "txns"},
                "S3Configuration": {
                    "RoleARN": ROLE,
                    "BucketARN": f"arn:aws:s3:::{BUCKET}",
                    "Prefix": "rs/",
                    "BufferingHints": {"IntervalInSeconds": 1, "SizeInMBs": 5},
                },
            },
        )
        try:
            for rid, amt in ((1, 10), (2, 20), (3, 30)):
                fh.put_record(
                    DeliveryStreamName="to-redshift",
                    Record={"Data": (json.dumps({"id": rid, "amt": amt}) + "\n").encode()},
                )

            rows = []
            for _ in range(40):
                with psycopg.connect(dsn) as conn:
                    rows = conn.execute("SELECT id, amt FROM txns ORDER BY id").fetchall()
                if len(rows) == 3:
                    break
                time.sleep(0.5)
            assert [(r[0], int(r[1])) for r in rows] == [(1, 10), (2, 20), (3, 30)]

            # the batch was also staged to S3 before the COPY
            staged = s3.list_objects_v2(Bucket=BUCKET, Prefix="rs/").get("Contents", [])
            assert staged
        finally:
            fh.delete_delivery_stream(DeliveryStreamName="to-redshift")
    finally:
        try:
            docker_client.containers.get("oblako-fh-pg").remove(force=True)
        except Exception:
            pass
