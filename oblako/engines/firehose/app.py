"""Local Amazon Data Firehose: delivery streams that buffer records to S3 or Redshift.

Serves the Firehose wire protocol (JSON 1.1, ``X-Amz-Target: Firehose_20150804.*``)
so an unmodified boto3 ``firehose`` client works: Create/Describe/List/Delete
DeliveryStream plus PutRecord/PutRecordBatch for the DirectPut source. Each stream
runs a delivery loop that buffers records and flushes them on its buffering
interval or size, as Firehose does:

* **S3 objects are named as on AWS**: ``<evaluated prefix><name>-<version>-
  yyyy-MM-dd-HH-mm-ss-<uuid><extension>``. A prefix without a ``!{timestamp:...}``
  expression gets ``yyyy/MM/dd/HH/`` appended; ``!{timestamp:<pattern>}`` and
  ``!{firehose:random-string}`` are evaluated, with the arrival time of the
  oldest record in the object. GZIP adds ``.gz``, Parquet ``.parquet``.
* **Record format conversion** (``DataFormatConversionConfiguration``): JSON
  records are converted to Parquet with the column types of a Glue Data Catalog
  table, read from oblako's Glue engine. Records that don't convert go to the
  ``ErrorOutputPrefix`` with ``!{firehose:error-output-type}`` =
  ``format-conversion-failed``, as Firehose writes them.
* **Redshift destination**: each batch is staged to S3, then loaded with the
  ``COPY`` statement Firehose issues, ``COPY <table> [(<columns>)] FROM
  's3://...' CREDENTIALS 'aws_iam_role=...' <CopyOptions>``, sent to the cluster
  in ``ClusterJDBCURL`` (oblako's Redshift runs it against S3). So the
  ``CopyOptions`` matter as on AWS: JSON records need ``JSON 'auto'``.
* **Kinesis source** (``KinesisStreamAsSource``): read from ``LATEST``, as
  Firehose starts reading a source stream.

Delivery stream definitions persist in ``~/.oblako/firehose/streams.json`` and
their loops restart with the engine; records still buffered are lost, as are
records in a deleted stream's buffer. Real Firehose floors the buffering
interval at 0 or 60 seconds by destination; oblako honours the configured value.
"""

from __future__ import annotations

import base64
import datetime
import gzip
import importlib.util
import io
import json
import os
import re
import secrets
import string
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from oblako import config, ports

if TYPE_CHECKING:
    from typing_extensions import LiteralString

_TARGET_PREFIX = "Firehose_20150804"
_JSON = "application/x-amz-json-1.1"
STATE = Path.home() / ".oblako" / "firehose" / "streams.json"
_EXPRESSION = re.compile(r"!\{([A-Za-z]+):([^}]*)\}")
_ALPHABET = string.ascii_lowercase + string.digits


class InvalidArgument(ValueError):
    """A request Firehose would reject with InvalidArgumentException."""


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
    kwargs = {"endpoint_url": endpoint, "config": cfg, "region_name": config.region()}
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        kwargs["aws_access_key_id"] = "oblako"
        kwargs["aws_secret_access_key"] = "oblako"
    return boto3.client("s3", **kwargs)


# ---------------------------------------------------------------------------
# Prefixes and object names
# ---------------------------------------------------------------------------
_JAVA_FIELDS = [
    ("yyyy", "%Y"),
    ("yy", "%y"),
    ("MM", "%m"),
    ("dd", "%d"),
    ("DDD", "%j"),
    ("HH", "%H"),
    ("mm", "%M"),
    ("ss", "%S"),
]


def _java_format(pattern: str, when: datetime.datetime) -> str:
    """Format ``when`` with a Java DateTimeFormatter pattern (the common fields).

    Text in single quotes is literal, as in DateTimeFormatter.
    """
    out, i = [], 0
    while i < len(pattern):
        if pattern[i] == "'":
            close = pattern.find("'", i + 1)
            close = len(pattern) if close < 0 else close
            out.append(pattern[i + 1 : close])
            i = close + 1
            continue
        for token, directive in _JAVA_FIELDS:
            if pattern.startswith(token, i):
                out.append(when.strftime(directive))
                i += len(token)
                break
        else:
            out.append(pattern[i])
            i += 1
    return "".join(out)


def _random_string() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(11))


def evaluate_prefix(
    prefix: str, when: datetime.datetime, error_type: str | None = None
) -> str:
    """Evaluate a Firehose Prefix or ErrorOutputPrefix at ``when``."""
    if "!{timestamp:" not in prefix:
        prefix = prefix + "!{timestamp:yyyy/MM/dd/HH/}"

    def value(m: re.Match) -> str:
        namespace, arg = m.group(1), m.group(2)
        if namespace == "timestamp":
            return _java_format(arg, when)
        if namespace == "firehose" and arg == "random-string":
            return _random_string()
        if namespace == "firehose" and arg == "error-output-type":
            return error_type or ""
        raise InvalidArgument(f"unsupported prefix expression !{{{namespace}:{arg}}}")

    return _EXPRESSION.sub(value, prefix)


def _check_prefixes(prefix: str, error_prefix: str | None, destination: str) -> None:
    """Reject prefixes Firehose rejects (its semantic rules)."""
    if "!{firehose:error-output-type}" in prefix:
        raise InvalidArgument("Prefix can't contain !{firehose:error-output-type}")
    if destination == "redshift" and "!{" in prefix:
        raise InvalidArgument("Prefix must not contain expressions for Redshift")
    if "!{" in prefix and not error_prefix:
        raise InvalidArgument(
            "ErrorOutputPrefix can't be null when Prefix contains expressions"
        )
    if error_prefix and "!{" in error_prefix:
        if "!{firehose:error-output-type}" not in error_prefix:
            raise InvalidArgument(
                "ErrorOutputPrefix expressions must include "
                "!{firehose:error-output-type}"
            )
    for value in (prefix, error_prefix or ""):
        for namespace, _ in _EXPRESSION.findall(value):
            if namespace not in ("timestamp", "firehose"):
                raise InvalidArgument(
                    f"prefix namespace {namespace} (dynamic partitioning) is not "
                    "supported by oblako"
                )


def object_name(
    prefix: str, stream: str, version: int, when: datetime.datetime, extension: str
) -> str:
    """Return ``<evaluated prefix><stream>-<version>-<timestamp>-<uuid><ext>``."""
    return (
        f"{evaluate_prefix(prefix, when)}{stream}-{version}-"
        f"{when:%Y-%m-%d-%H-%M-%S}-{uuid.uuid4()}{extension}"
    )


# ---------------------------------------------------------------------------
# Record format conversion: JSON -> Parquet with a Glue table's schema
# ---------------------------------------------------------------------------
def _glue_columns(schema_conf: dict) -> list[tuple[str, str]]:
    import boto3

    kwargs = {
        "region_name": schema_conf.get("Region") or config.region(),
        "endpoint_url": os.environ.get("AWS_ENDPOINT_URL_GLUE")
        or f"http://localhost:{ports.GLUE_CATALOG}",
    }
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        kwargs["aws_access_key_id"] = "oblako"
        kwargs["aws_secret_access_key"] = "oblako"
    glue = boto3.client("glue", **kwargs)
    table = glue.get_table(
        DatabaseName=schema_conf["DatabaseName"], Name=schema_conf["TableName"]
    )["Table"]
    columns = table["StorageDescriptor"]["Columns"]
    return [(c["Name"], c["Type"].strip().lower()) for c in columns]


def _arrow_type(hive: str):
    import pyarrow as pa

    simple = {
        "string": pa.string(),
        "varchar": pa.string(),
        "char": pa.string(),
        "boolean": pa.bool_(),
        "tinyint": pa.int8(),
        "smallint": pa.int16(),
        "int": pa.int32(),
        "integer": pa.int32(),
        "bigint": pa.int64(),
        "float": pa.float32(),
        "double": pa.float64(),
        "date": pa.date32(),
        "timestamp": pa.timestamp("ms"),
        "binary": pa.binary(),
    }
    base = hive.split("(")[0]
    if base in simple:
        return simple[base]
    m = re.fullmatch(r"decimal\((\d+),\s*(\d+)\)", hive)
    if m:
        return pa.decimal128(int(m.group(1)), int(m.group(2)))
    raise InvalidArgument(f"oblako can't convert Glue column type {hive!r} to Parquet")


def _parse_timestamp(value):
    if isinstance(value, (int, float)):
        seconds = value / 1000 if abs(value) > 1e11 else value
        return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    text = str(value).replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(text.replace(" ", "T", 1))


def _coerce(value, hive: str):
    if value is None:
        return None
    base = hive.split("(")[0]
    if base == "timestamp":
        return _parse_timestamp(value)
    if base == "date":
        return datetime.date.fromisoformat(str(value)[:10])
    if base == "decimal":
        import decimal

        return decimal.Decimal(str(value))
    if base in ("tinyint", "smallint", "int", "integer", "bigint"):
        if isinstance(value, bool) or (isinstance(value, float) and value % 1):
            raise ValueError(f"{value!r} is not an integer")
        return int(value)
    if base in ("float", "double"):
        return float(value)
    if base == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{value!r} is not a boolean")
        return value
    if base == "binary":
        return base64.b64decode(value)
    return value if isinstance(value, str) else json.dumps(value)


def _iter_json_records(payload: bytes):
    """Yield the JSON objects concatenated in a batch (with or without newlines)."""
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


def to_parquet(
    records: list[bytes], columns: list[tuple[str, str]], compression: str
) -> tuple[bytes | None, list[tuple[bytes, str]]]:
    """Convert JSON records to one Parquet file; return (file, failed records)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema([(name, _arrow_type(hive)) for name, hive in columns])
    rows, failed = [], []
    for raw in records:
        try:
            parsed = list(_iter_json_records(raw))
            if len(parsed) != 1 or not isinstance(parsed[0], dict):
                raise ValueError("a record must be one JSON object")
            # OpenX JSON SerDe matches keys to columns case-insensitively
            lowered = {k.lower(): v for k, v in parsed[0].items()}
            rows.append(
                {
                    name: _coerce(lowered.get(name.lower()), hive)
                    for name, hive in columns
                }
            )
        except (ValueError, TypeError, ArithmeticError) as e:
            failed.append((raw, str(e)))
    if not rows:
        return None, failed
    table = pa.Table.from_pylist(rows, schema=schema)
    sink = io.BytesIO()
    codec = {"UNCOMPRESSED": "none"}.get(compression, compression.lower())
    pq.write_table(table, sink, compression=codec)
    return sink.getvalue(), failed


def _error_record(raw: bytes, code: str, message: str) -> bytes:
    """One failed record as Firehose writes it under the ErrorOutputPrefix."""
    return (
        json.dumps(
            {
                "attemptsMade": 1,
                "arrivalTimestamp": int(_now().timestamp() * 1000),
                "lastErrorCode": code,
                "lastErrorMessage": message,
                "rawData": base64.b64encode(raw).decode(),
            }
        )
        + "\n"
    ).encode()


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------
class FirehoseExecutor:
    """Tracks delivery streams and runs a buffer/flush loop per stream."""

    def __init__(self, state_path: Path | None = None):
        """Load persisted delivery streams and restart their delivery loops."""
        self._streams: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.state_path = state_path or STATE
        for request in self._load().values():
            try:
                self._start(request, persist=False)
            except Exception:
                continue  # a stream that can't start again stays out

    def _load(self) -> dict[str, dict]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def _persist(self) -> None:
        with self._lock:
            requests = {n: s["request"] for n, s in self._streams.items()}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(requests, indent=1, default=str))
        tmp.replace(self.state_path)

    def create_delivery_stream(self, req: dict) -> str:
        """Register a delivery stream and start its delivery loop; return its ARN.

        Destination is S3 (Extended/legacy) or Redshift (S3 staging + COPY);
        source is DirectPut or a Kinesis stream (KinesisStreamAsSource).
        """
        name = req["DeliveryStreamName"]
        with self._lock:
            if name in self._streams:
                raise ResourceInUse(f"Firehose stream {name} already exists")
        return self._start(req, persist=True)

    def _start(self, req: dict, persist: bool) -> str:
        name = req["DeliveryStreamName"]
        arn = (
            f"arn:aws:firehose:{config.region()}:{config.account_id()}:"
            f"deliverystream/{name}"
        )
        redshift_conf = req.get("RedshiftDestinationConfiguration")
        extended = req.get("ExtendedS3DestinationConfiguration")
        s3_conf = (
            redshift_conf["S3Configuration"]
            if redshift_conf
            else extended or req.get("S3DestinationConfiguration")
        )
        if not s3_conf:
            raise InvalidArgument("only S3 and Redshift destinations are supported")
        destination = "redshift" if redshift_conf else "s3"
        compression = s3_conf.get("CompressionFormat", "UNCOMPRESSED")
        if compression not in ("UNCOMPRESSED", "GZIP"):
            raise InvalidArgument(
                f"CompressionFormat {compression} is not supported by oblako; "
                "use UNCOMPRESSED or GZIP"
            )
        prefix = s3_conf.get("Prefix", "")
        error_prefix = s3_conf.get("ErrorOutputPrefix")
        _check_prefixes(prefix, error_prefix, destination)

        conversion = (extended or {}).get("DataFormatConversionConfiguration") or {}
        parquet = None
        if conversion and conversion.get("Enabled", True):
            serializer = conversion["OutputFormatConfiguration"]["Serializer"]
            if "ParquetSerDe" not in serializer:
                raise InvalidArgument("oblako converts records to Parquet only")
            if compression != "UNCOMPRESSED":
                raise InvalidArgument(
                    "CompressionFormat must be UNCOMPRESSED with record format "
                    "conversion"
                )
            if importlib.util.find_spec("pyarrow") is None:
                raise InvalidArgument(
                    "record format conversion needs pyarrow: "
                    "pip install 'oblako[parquet]'"
                )
            parquet = {
                "schema": conversion["SchemaConfiguration"],
                "compression": serializer["ParquetSerDe"].get("Compression", "SNAPPY"),
            }
        buffering = s3_conf.get("BufferingHints") or {}
        default_size = 128 if parquet else 5
        size_mb = int(buffering.get("SizeInMBs", default_size))
        if parquet and size_mb < 64:
            raise InvalidArgument(
                "BufferingHints.SizeInMBs can't be less than 64 with record format "
                "conversion"
            )
        extension = s3_conf.get("FileExtension") or (
            ".parquet" if parquet else ".gz" if compression == "GZIP" else ""
        )
        stream = {
            "name": name,
            "arn": arn,
            "request": req,
            "type": req.get("DeliveryStreamType", "DirectPut"),
            "destination": destination,
            "bucket": s3_conf["BucketARN"].split(":::")[-1],
            "prefix": prefix,
            "error_prefix": error_prefix,
            "gzip": compression == "GZIP",
            "compression": compression,
            "extension": extension,
            "parquet": parquet,
            "redshift": redshift_conf,
            "source": req.get("KinesisStreamSourceConfiguration"),
            "interval": int(buffering.get("IntervalInSeconds", 300)),
            "size_mb": size_mb,
            "size_bytes": size_mb * 1024 * 1024,
            "buffer": [],
            "oldest": None,
            "stop": threading.Event(),
            "created": _now(),
            "lock": threading.Lock(),
        }
        with self._lock:
            self._streams[name] = stream
        if persist:
            self._persist()
        threading.Thread(target=self._deliver_loop, args=(name,), daemon=True).start()
        if stream["source"]:
            # positioned before create returns, so the next record put is read
            iterators = self._latest_iterators(stream)
            threading.Thread(
                target=self._consume_kinesis, args=(name, iterators), daemon=True
            ).start()
        return arn

    def _deliver_loop(self, name: str) -> None:
        """Flush a stream's buffer on its buffering interval until deleted."""
        stream = self._streams[name]
        while not stream["stop"].wait(timeout=min(stream["interval"], 5) or 0.5):
            oldest = stream["oldest"]
            if oldest and (_now() - oldest).total_seconds() >= stream["interval"]:
                self._safe_flush(stream)
        self._safe_flush(stream)  # final drain on delete

    def _safe_flush(self, stream: dict) -> None:
        try:
            self._flush(stream)
        except Exception as e:
            print(f"firehose {stream['name']}: delivery failed: {e}", flush=True)

    @staticmethod
    def _latest_iterators(stream: dict) -> dict[str, str]:
        """Shard iterators at LATEST, where Firehose starts reading a source stream."""
        kinesis = _kinesis_client()
        source_name = stream["source"]["KinesisStreamARN"].split("/")[-1]
        shards = kinesis.describe_stream(StreamName=source_name)["StreamDescription"][
            "Shards"
        ]
        return {
            s["ShardId"]: kinesis.get_shard_iterator(
                StreamName=source_name,
                ShardId=s["ShardId"],
                ShardIteratorType="LATEST",
            )["ShardIterator"]
            for s in shards
        }

    def _consume_kinesis(self, name: str, iterators: dict[str, str]) -> None:
        """Poll the source Kinesis stream and buffer its records."""
        stream = self._streams[name]
        kinesis = _kinesis_client()
        while not stream["stop"].wait(timeout=0.5):
            for shard_id, iterator in list(iterators.items()):
                try:
                    resp = kinesis.get_records(ShardIterator=iterator, Limit=500)
                except Exception:
                    continue
                for record in resp.get("Records", []):
                    self._buffer(stream, record["Data"])
                iterators[shard_id] = resp.get("NextShardIterator", iterator)

    def _buffer(self, stream: dict, data: bytes) -> None:
        with stream["lock"]:
            if not stream["buffer"]:
                stream["oldest"] = _now()
            stream["buffer"].append(data)
            buffered = sum(len(r) for r in stream["buffer"])
        if buffered >= stream["size_bytes"]:
            self._safe_flush(stream)

    def _flush(self, stream: dict) -> None:
        """Deliver the buffered records: an S3 object, then COPY for Redshift."""
        with stream["lock"]:
            if not stream["buffer"]:
                return
            records, when = stream["buffer"], stream["oldest"] or _now()
            stream["buffer"], stream["oldest"] = [], None
        s3 = _s3_client()
        if stream["parquet"]:
            columns = _glue_columns(stream["parquet"]["schema"])
            body, failed = to_parquet(
                records, columns, stream["parquet"]["compression"]
            )
            if failed:
                self._write_errors(stream, failed, when)
            if body is None:
                return
        else:
            payload = b"".join(records)
            body = gzip.compress(payload) if stream["gzip"] else payload
        key = object_name(
            stream["prefix"], stream["name"], 1, when, stream["extension"]
        )
        s3.put_object(Bucket=stream["bucket"], Key=key, Body=body)
        if stream["destination"] == "redshift":
            _copy_into_redshift(stream["redshift"], stream["bucket"], key)

    def _write_errors(
        self, stream: dict, failed: list[tuple[bytes, str]], when: datetime.datetime
    ) -> None:
        error_type = "format-conversion-failed"
        error_prefix = stream["error_prefix"] or (
            stream["prefix"] + error_type + "/!{timestamp:yyyy/MM/DDD/HH/}"
        )
        prefix = evaluate_prefix(error_prefix, when, error_type)
        key = f"{prefix}{stream['name']}-1-{when:%Y-%m-%d-%H-%M-%S}-{uuid.uuid4()}"
        body = b"".join(
            _error_record(raw, "DataFormatConversion.MalformedData", message)
            for raw, message in failed
        )
        _s3_client().put_object(Bucket=stream["bucket"], Key=key, Body=body)

    def put_record(self, name: str, data: bytes) -> str:
        """Buffer one record; return its RecordId."""
        stream = self._require(name)
        if stream["source"]:
            raise InvalidArgument(
                "PutRecord is not supported for a KinesisStreamAsSource stream"
            )
        self._buffer(stream, data)
        return uuid.uuid4().hex

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
                "VersionId": "1",
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
            "CompressionFormat": stream["compression"],
            "BufferingHints": {
                "IntervalInSeconds": stream["interval"],
                "SizeInMBs": stream["size_mb"],
            },
        }
        if stream["error_prefix"]:
            s3_desc["ErrorOutputPrefix"] = stream["error_prefix"]
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
        extended = stream["request"].get("ExtendedS3DestinationConfiguration") or {}
        if extended.get("DataFormatConversionConfiguration"):
            s3_desc["DataFormatConversionConfiguration"] = extended[
                "DataFormatConversionConfiguration"
            ]
        return {"ExtendedS3DestinationDescription": s3_desc}

    def list_delivery_streams(self) -> dict:
        """Return the delivery-stream names."""
        with self._lock:
            names = list(self._streams)
        return {"DeliveryStreamNames": names, "HasMoreDeliveryStreams": False}

    def delete_delivery_stream(self, name: str) -> None:
        """Stop a stream's delivery loop and drop it."""
        with self._lock:
            stream = self._streams.pop(name, None)
        if stream is None:
            raise KeyError(name)
        stream["stop"].set()
        self._persist()

    def _require(self, name: str) -> dict:
        with self._lock:
            stream = self._streams.get(name)
        if stream is None:
            raise KeyError(name)
        return stream


class ResourceInUse(Exception):
    """A stream with that name already exists."""


def _decode(data) -> bytes:
    """Return a record's Data as bytes (base64 on the wire, as boto3 sends it)."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    return base64.b64decode(data)


def _kinesis_client():
    """boto3 Kinesis client for the local Kinesis Data Streams (source), host-side."""
    import boto3

    endpoint = os.environ.get("AWS_ENDPOINT_URL_KINESIS") or "http://localhost:4567"
    kwargs = {"endpoint_url": endpoint, "region_name": config.region()}
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        kwargs["aws_access_key_id"] = "oblako"
        kwargs["aws_secret_access_key"] = "oblako"
    return boto3.client("kinesis", **kwargs)


def _jdbc_to_dsn(jdbc_url: str, username: str, password: str) -> str:
    """Turn Firehose's ClusterJDBCURL into a libpq DSN."""
    hostport_db = jdbc_url.removeprefix("jdbc:redshift://")
    hostport, _, database = hostport_db.partition("/")
    return f"postgresql://{username}:{password}@{hostport}/{database}"


def copy_statement(conf: dict, bucket: str, key: str) -> str:
    """Return the COPY statement Firehose issues for one staged object."""
    command = conf["CopyCommand"]
    columns = command.get("DataTableColumns")
    target = command["DataTableName"] + (f" ({columns})" if columns else "")
    role = conf.get("RoleARN", "")
    options = command.get("CopyOptions", "")
    return (
        f"COPY {target} FROM 's3://{bucket}/{key}' "
        f"CREDENTIALS 'aws_iam_role={role}' {options}"
    ).strip()


def _copy_into_redshift(conf: dict, bucket: str, key: str) -> None:
    """Run Firehose's COPY on the cluster in ClusterJDBCURL."""
    import psycopg

    dsn = _jdbc_to_dsn(conf["ClusterJDBCURL"], conf["Username"], conf["Password"])
    # the statement is Firehose's own COPY, built from the stream's configuration
    statement = cast("LiteralString", copy_statement(conf, bucket, key))
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(statement)


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
                f"Firehose stream {err} not found",
                status=400,
            )
        except ResourceInUse as err:
            return _error("ResourceInUseException", str(err))
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
