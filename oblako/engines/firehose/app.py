"""Local Amazon Data Firehose: delivery streams that buffer records to S3.

Serves the Firehose wire protocol (JSON 1.1, ``X-Amz-Target: Firehose_20150804.*``)
so an unmodified boto3 ``firehose`` client works: Create/Describe/List/Delete
DeliveryStream plus PutRecord/PutRecordBatch for the DirectPut source. Each stream
runs a delivery loop that buffers incoming records and stages them as one S3
object per flush (the Firehose ``prefix/YYYY/MM/DD/HH/name-ts-uuid`` layout),
flushing on the configured buffering interval or size.

Scope: DirectPut source + S3 destination (Extended/legacy). Real Firehose floors
the buffering interval at 60s; oblako honors the configured value so a short
interval works locally.
"""

from __future__ import annotations

import datetime
import gzip
import json
import os
import threading
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

_TARGET_PREFIX = "Firehose_20150804"
_JSON = "application/x-amz-json-1.1"
_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
_ACCOUNT = "000000000000"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _s3_client():
    """boto3 S3 client for the local object store (S3Proxy), host-side."""
    import boto3
    from botocore.config import Config

    endpoint = (
        os.environ.get("AWS_ENDPOINT_URL_S3")
        or os.environ.get("OBLAKO_S3_ENDPOINT")
        or "http://localhost:9000"
    )
    try:
        cfg = Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        )
    except TypeError:
        cfg = Config(s3={"addressing_style": "path"})
    kwargs = {"endpoint_url": endpoint, "config": cfg, "region_name": _REGION}
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        kwargs["aws_access_key_id"] = "oblako"
        kwargs["aws_secret_access_key"] = "oblako"
    return boto3.client("s3", **kwargs)


class FirehoseExecutor:
    """Tracks delivery streams and runs a buffer/flush loop per stream to S3."""

    def __init__(self):
        """Initialize the in-memory delivery-stream registry."""
        self._streams: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create_delivery_stream(self, req: dict) -> str:
        """Register a delivery stream and start its delivery loop; return ARN.

        Destination is S3 (Extended/legacy) or Redshift (S3 staging + COPY);
        source is DirectPut or a Kinesis stream (KinesisStreamAsSource).
        """
        name = req["DeliveryStreamName"]
        arn = f"arn:aws:firehose:{_REGION}:{_ACCOUNT}:deliverystream/{name}"
        redshift_conf = req.get("RedshiftDestinationConfiguration")
        s3_conf = (
            redshift_conf["S3Configuration"]
            if redshift_conf
            else req.get("ExtendedS3DestinationConfiguration")
            or req.get("S3DestinationConfiguration")
        )
        if not s3_conf:
            raise ValueError(
                "only S3 and Redshift destinations are supported"
            )
        buffering = s3_conf.get("BufferingHints") or {}
        stream = {
            "name": name,
            "arn": arn,
            "type": req.get("DeliveryStreamType", "DirectPut"),
            "destination": "redshift" if redshift_conf else "s3",
            "bucket": s3_conf["BucketARN"].split(":::")[-1],
            "prefix": s3_conf.get("Prefix", ""),
            "gzip": s3_conf.get("CompressionFormat", "UNCOMPRESSED") == "GZIP",
            "redshift": redshift_conf,
            "source": req.get("KinesisStreamSourceConfiguration"),
            "interval": int(buffering.get("IntervalInSeconds", 60)),
            "size_bytes": int(buffering.get("SizeInMBs", 5)) * 1024 * 1024,
            "buffer": [],
            "stop": threading.Event(),
            "created": _now(),
            "lock": threading.Lock(),
        }
        with self._lock:
            self._streams[name] = stream
        threading.Thread(target=self._deliver_loop, args=(name,), daemon=True).start()
        if stream["source"]:
            threading.Thread(
                target=self._consume_kinesis, args=(name,), daemon=True
            ).start()
        return arn

    def _deliver_loop(self, name: str) -> None:
        """Flush a stream's buffer on its buffering interval until deleted."""
        stream = self._streams[name]
        while not stream["stop"].wait(timeout=min(stream["interval"], 5)):
            self._flush(stream)
        self._flush(stream)  # final drain on delete

    def _consume_kinesis(self, name: str) -> None:
        """Poll the source Kinesis stream and buffer its records (KinesisStreamAsSource)."""
        stream = self._streams[name]
        kinesis = _kinesis_client()
        source_name = stream["source"]["KinesisStreamARN"].split("/")[-1]
        shards = kinesis.describe_stream(StreamName=source_name)[
            "StreamDescription"
        ]["Shards"]
        iterators = {
            s["ShardId"]: kinesis.get_shard_iterator(
                StreamName=source_name,
                ShardId=s["ShardId"],
                ShardIteratorType="TRIM_HORIZON",
            )["ShardIterator"]
            for s in shards
        }
        while not stream["stop"].wait(timeout=0.5):
            for shard_id, iterator in list(iterators.items()):
                try:
                    resp = kinesis.get_records(ShardIterator=iterator, Limit=500)
                except Exception:
                    continue
                for record in resp.get("Records", []):
                    with stream["lock"]:
                        stream["buffer"].append(record["Data"])
                iterators[shard_id] = resp.get("NextShardIterator", iterator)

    def _flush(self, stream: dict) -> None:
        """Stage the buffered records to S3, and COPY into Redshift if configured."""
        with stream["lock"]:
            if not stream["buffer"]:
                return
            payload = b"".join(stream["buffer"])
            stream["buffer"] = []
        now = _now()
        key = (
            f"{stream['prefix']}{now:%Y/%m/%d/%H}/"
            f"{stream['name']}-{now:%Y-%m-%d-%H-%M-%S}-{uuid.uuid4()}"
        )
        body = gzip.compress(payload) if stream["gzip"] else payload
        _s3_client().put_object(Bucket=stream["bucket"], Key=key, Body=body)
        if stream["destination"] == "redshift":
            _load_redshift(stream["redshift"], payload)

    def put_record(self, name: str, data: bytes) -> str:
        """Buffer one record; return its RecordId."""
        stream = self._require(name)
        if stream["source"]:
            raise ValueError(
                "PutRecord is not supported for a KinesisStreamAsSource stream"
            )
        record_id = uuid.uuid4().hex
        with stream["lock"]:
            stream["buffer"].append(data)
            buffered = sum(len(r) for r in stream["buffer"])
        if buffered >= stream["size_bytes"]:
            self._flush(stream)
        return record_id

    def put_record_batch(self, name: str, records: list[dict]) -> list[dict]:
        """Buffer a batch of records; return a RecordId per record."""
        return [
            {"RecordId": self.put_record(name, _decode(r["Data"]))} for r in records
        ]

    def describe_delivery_stream(self, name: str) -> dict | None:
        """Return a delivery stream description, or None if unknown."""
        with self._lock:
            stream = self._streams.get(name)
        if stream is None:
            return None
        return {
            "DeliveryStreamDescription": {
                "DeliveryStreamName": stream["name"],
                "DeliveryStreamARN": stream["arn"],
                "DeliveryStreamStatus": "ACTIVE",
                "DeliveryStreamType": stream["type"],
                "CreateTimestamp": stream["created"],
                "HasMoreDestinations": False,
                "Destinations": [
                    {
                        "DestinationId": "destinationId-000000000001",
                        **self._destination_description(stream),
                    }
                ],
            }
        }

    @staticmethod
    def _destination_description(stream: dict) -> dict:
        """Build the destination-specific description block for describe."""
        s3_desc = {
            "BucketARN": f"arn:aws:s3:::{stream['bucket']}",
            "Prefix": stream["prefix"],
            "CompressionFormat": "GZIP" if stream["gzip"] else "UNCOMPRESSED",
            "BufferingHints": {
                "IntervalInSeconds": stream["interval"],
                "SizeInMBs": stream["size_bytes"] // (1024 * 1024),
            },
        }
        if stream["destination"] == "redshift":
            redshift = stream["redshift"]
            return {
                "RedshiftDestinationDescription": {
                    "ClusterJDBCURL": redshift.get("ClusterJDBCURL"),
                    "CopyCommand": redshift.get("CopyCommand", {}),
                    "Username": redshift.get("Username"),
                    "S3DestinationDescription": s3_desc,
                }
            }
        return {"ExtendedS3DestinationDescription": s3_desc}

    def list_delivery_streams(self) -> dict:
        """Return the delivery-stream names."""
        with self._lock:
            names = list(self._streams)
        return {"DeliveryStreamNames": names, "HasMoreDeliveryStreams": False}

    def delete_delivery_stream(self, name: str) -> None:
        """Stop a stream's delivery loop and drop it (idempotent)."""
        with self._lock:
            stream = self._streams.pop(name, None)
        if stream:
            stream["stop"].set()

    def _require(self, name: str) -> dict:
        with self._lock:
            stream = self._streams.get(name)
        if stream is None:
            raise KeyError(name)
        return stream


def _decode(data) -> bytes:
    """A Firehose record's Data is base64 on the wire; boto3 sends it that way."""
    import base64

    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    return base64.b64decode(data)


def _kinesis_client():
    """boto3 Kinesis client for the local Kinesis Data Streams (source), host-side."""
    import boto3

    endpoint = os.environ.get("AWS_ENDPOINT_URL_KINESIS") or "http://localhost:4567"
    kwargs = {"endpoint_url": endpoint, "region_name": _REGION}
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        kwargs["aws_access_key_id"] = "oblako"
        kwargs["aws_secret_access_key"] = "oblako"
    return boto3.client("kinesis", **kwargs)


def _jdbc_to_dsn(jdbc_url: str, username: str, password: str) -> str:
    """Turn Firehose's ClusterJDBCURL into a psycopg2 DSN."""
    hostport_db = jdbc_url.removeprefix("jdbc:redshift://")
    hostport, _, database = hostport_db.partition("/")
    return f"postgresql://{username}:{password}@{hostport}/{database}"


def _iter_json_records(payload: bytes):
    """Yield the JSON objects concatenated in a staged batch (with or without newlines)."""
    decoder = json.JSONDecoder()
    text = payload.decode()
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        record, pos = decoder.raw_decode(text, pos)
        yield record


def _load_redshift(conf: dict, payload: bytes) -> None:
    """COPY one staged batch into a Redshift (Postgres) destination table.

    Real Redshift COPYs staged JSON straight off S3 (``FORMAT AS JSON 'auto'``);
    oblako's warehouse is Postgres, whose COPY has no JSON mode, so the batch is
    reshaped to CSV against the table's column order (the same mapping semantics)
    and loaded over ``COPY ... FROM STDIN`` - mirroring the book's firehose-local.
    """
    import csv
    import io

    import psycopg

    dsn = _jdbc_to_dsn(conf["ClusterJDBCURL"], conf["Username"], conf["Password"])
    table = conf["CopyCommand"]["DataTableName"]
    with psycopg.connect(dsn) as conn:
        columns = [
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s ORDER BY ordinal_position",
                (table,),
            ).fetchall()
        ]
        if not columns:
            raise RuntimeError(f"destination table {table} does not exist")
        rows = io.StringIO()
        writer = csv.DictWriter(rows, fieldnames=columns, extrasaction="ignore")
        for record in _iter_json_records(payload):
            writer.writerow({c: record.get(c) for c in columns})
        with conn.cursor() as cur, cur.copy(
            f"COPY {table} FROM STDIN WITH (FORMAT csv)"
        ) as copy:
            copy.write(rows.getvalue())


def _json_response(payload: dict, status: int = 200) -> Response:
    def default(obj):
        if isinstance(obj, datetime.datetime):
            return obj.timestamp()
        raise TypeError

    return Response(
        json.dumps(payload, default=default), status_code=status, media_type=_JSON
    )


def _error(code: str, message: str, status: int = 400) -> Response:
    return Response(
        json.dumps({"__type": code, "message": message}),
        status_code=status,
        media_type=_JSON,
        headers={"X-Amzn-Errortype": code},
    )


class FirehoseApp:
    """Dispatches Firehose operations by X-Amz-Target to the executor."""

    def __init__(self, executor: FirehoseExecutor):
        """Bind the dispatcher to an executor."""
        self.executor = executor

    async def handle(self, request: Request) -> Response:
        """Dispatch one Firehose request by its X-Amz-Target operation."""
        op = request.headers.get("x-amz-target", "").split(".")[-1]
        body = await request.body()
        try:
            req = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _error("SerializationException", "invalid JSON body")
        try:
            return getattr(self, f"op_{op}")(req)
        except AttributeError:
            return _error("UnknownOperationException", f"unknown op {op!r}")
        except KeyError as err:
            return _error(
                "ResourceNotFoundException",
                f"delivery stream {err} not found",
                status=400,
            )
        except Exception as err:
            return _error("InvalidArgumentException", str(err))

    def op_CreateDeliveryStream(self, req: dict) -> Response:
        """Create a delivery stream."""
        return _json_response(
            {"DeliveryStreamARN": self.executor.create_delivery_stream(req)}
        )

    def op_DescribeDeliveryStream(self, req: dict) -> Response:
        """Describe a delivery stream."""
        desc = self.executor.describe_delivery_stream(req["DeliveryStreamName"])
        if desc is None:
            raise KeyError(req.get("DeliveryStreamName"))
        return _json_response(desc)

    def op_ListDeliveryStreams(self, req: dict) -> Response:
        """List delivery streams."""
        return _json_response(self.executor.list_delivery_streams())

    def op_DeleteDeliveryStream(self, req: dict) -> Response:
        """Delete a delivery stream."""
        self.executor.delete_delivery_stream(req["DeliveryStreamName"])
        return _json_response({})

    def op_PutRecord(self, req: dict) -> Response:
        """Buffer one record for delivery."""
        record_id = self.executor.put_record(
            req["DeliveryStreamName"], _decode(req["Record"]["Data"])
        )
        return _json_response({"RecordId": record_id, "Encrypted": False})

    def op_PutRecordBatch(self, req: dict) -> Response:
        """Buffer a batch of records for delivery."""
        responses = self.executor.put_record_batch(
            req["DeliveryStreamName"], req["Records"]
        )
        return _json_response(
            {
                "FailedPutCount": 0,
                "Encrypted": False,
                "RequestResponses": responses,
            }
        )


def create_app(executor: FirehoseExecutor | None = None) -> Starlette:
    """Create the Starlette app for the local Firehose."""
    dispatcher = FirehoseApp(executor or FirehoseExecutor())
    return Starlette(routes=[Route("/", dispatcher.handle, methods=["POST"])])


app = create_app()
