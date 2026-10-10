"""Local Amazon Athena: the boto3 ``athena`` API executed via oblako's Trino.

AWS Athena runs a Trino/Presto-derived engine over S3; oblako already ships a
Trino container wired to the Iceberg REST catalog + S3Proxy, so this engine wraps
it in the Athena wire protocol (JSON 1.1, ``X-Amz-Target: AmazonAthena.*``):
StartQueryExecution runs the SQL through Trino and writes the results to S3 (the
Athena ``OutputLocation``, or the workgroup's); GetQueryExecution polls the
status; GetQueryResults returns the Athena ResultSet. So unmodified boto3
``athena`` code runs locally, and awswrangler's too: workgroups
(:mod:`.workgroups`) and CTAS with its data manifest (:mod:`.ctas`).

``AwsDataCatalog`` is Trino's ``awsdatacatalog`` catalog, the Hive connector over
oblako's Glue Data Catalog; Iceberg tables in it are redirected to ``iceberg``.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import os
import threading
import time
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from . import catalogs
from .ctas import rewrite, statement_type
from .workgroups import DEFAULT_OUTPUT, WorkGroupError, WorkGroups

_JSON = "application/x-amz-json-1.1"
_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def _now() -> datetime.datetime:
    """Return the current UTC time."""
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


def _split_uri(uri: str) -> tuple[str, str]:
    """Split an ``s3://bucket/key`` URI into (bucket, key)."""
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    return bucket, key


class AthenaExecutor:
    """Runs Athena queries through Trino and tracks their state + results."""

    def __init__(self, workgroups: WorkGroups | None = None):
        """Initialize the in-memory query registry."""
        self._queries: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.workgroups = workgroups or WorkGroups()

    def start_query_execution(self, req: dict) -> str:
        """Register and run a query; return its QueryExecutionId."""
        query_id = uuid.uuid4().hex
        context = req.get("QueryExecutionContext") or {}
        workgroup = req.get("WorkGroup") or "primary"
        output = self.workgroups.output_location(
            workgroup, (req.get("ResultConfiguration") or {}).get("OutputLocation")
        )
        if output.rstrip("/") == DEFAULT_OUTPUT.rstrip("/"):
            _ensure_bucket(output)
        catalog = (context.get("Catalog") or catalogs.DEFAULT_CATALOG).lower()
        record = {
            "QueryExecutionId": query_id,
            "Query": req["QueryString"],
            "Catalog": catalog,
            "Database": context.get("Database"),
            "OutputDir": output.rstrip("/"),
            "OutputLocation": f"{output.rstrip('/')}/{query_id}.csv",
            "WorkGroup": workgroup,
            "StatementType": statement_type(req["QueryString"]),
            "State": "QUEUED",
            "SubmissionDateTime": _now(),
            "columns": [],
            "types": [],
            "rows": [],
        }
        with self._lock:
            self._queries[query_id] = record
        threading.Thread(target=self._run, args=(query_id,), daemon=True).start()
        return query_id

    def _run(self, query_id: str) -> None:
        """Execute the query through Trino and write the results to S3."""
        record = self._queries[query_id]
        with self._lock:
            record["State"] = "RUNNING"
        started = time.monotonic()
        try:
            from oblako.services.trino import TrinoService

            trino = TrinoService()
            catalog, schema = catalogs.resolve(record["Catalog"], record["Database"])
            sql, session = catalogs.rewrite(record["Query"]), {}
            ctas = rewrite(sql, record["OutputDir"], query_id)
            if ctas is not None:
                sql, session = ctas.sql, ctas.session
                if ctas.iceberg:
                    catalog = "iceberg"
            result = trino.query(sql, catalog=catalog, schema=schema, session=session)
            if "error" in result:
                message = (result["error"] or {}).get("message", "query failed")
                raise RuntimeError(message)
            if ctas is not None:
                record["DataManifestLocation"] = self._write_manifest(
                    trino, record, ctas.table, catalog, schema
                )
            self._write_csv(record, result["columns"], result["rows"])
            with self._lock:
                record.update(
                    State="SUCCEEDED",
                    ElapsedMillis=int((time.monotonic() - started) * 1000),
                    CompletionDateTime=_now(),
                    columns=result["columns"],
                    types=result.get("types", []),
                    rows=result["rows"],
                )
        except Exception as err:
            with self._lock:
                record.update(
                    State="FAILED",
                    CompletionDateTime=_now(),
                    StateChangeReason=str(err).strip(),
                )

    @staticmethod
    def _write_manifest(
        trino, record: dict, table: str, catalog: str, schema: str | None
    ) -> str:
        """List a CTAS table's data files in ``<id>-manifest.csv``, as Athena does."""
        files = trino.query(
            f'SELECT DISTINCT "$path" FROM {table}',
            catalog=catalog,
            schema=schema,
        )
        if "error" in files:
            raise RuntimeError(
                files["error"].get("message", "listing the CTAS files failed")
            )
        location = f"{record['OutputDir']}/{record['QueryExecutionId']}-manifest.csv"
        bucket, key = _split_uri(location)
        body = "".join(f"{row[0]}\n" for row in sorted(files["rows"]))
        _s3_client().put_object(Bucket=bucket, Key=key, Body=body.encode())
        return location

    @staticmethod
    def _write_csv(record: dict, columns: list, rows: list) -> None:
        """Write the result set to the Athena OutputLocation as CSV (header + rows)."""
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
        bucket, key = _split_uri(record["OutputLocation"])
        _s3_client().put_object(Bucket=bucket, Key=key, Body=buffer.getvalue().encode())

    def get_query_execution(self, query_id: str) -> dict | None:
        """Return the QueryExecution record, or None if unknown."""
        with self._lock:
            record = self._queries.get(query_id)
            if record is None:
                return None
            status = {
                "State": record["State"],
                "SubmissionDateTime": record["SubmissionDateTime"],
            }
            if record.get("CompletionDateTime"):
                status["CompletionDateTime"] = record["CompletionDateTime"]
            if record.get("StateChangeReason"):
                status["StateChangeReason"] = record["StateChangeReason"]
            elapsed = record.get("ElapsedMillis", 0)
            statistics = {
                "EngineExecutionTimeInMillis": elapsed,
                "TotalExecutionTimeInMillis": elapsed,
                "QueryQueueTimeInMillis": 0,
                "DataScannedInBytes": 0,
            }
            if record.get("DataManifestLocation"):
                statistics["DataManifestLocation"] = record["DataManifestLocation"]
            return {
                "QueryExecution": {
                    "QueryExecutionId": query_id,
                    "Query": record["Query"],
                    "StatementType": record["StatementType"],
                    "Statistics": statistics,
                    "ResultConfiguration": {"OutputLocation": record["OutputLocation"]},
                    "QueryExecutionContext": {
                        "Database": record["Database"],
                        "Catalog": record["Catalog"],
                    },
                    "Status": status,
                    "WorkGroup": record["WorkGroup"],
                }
            }

    def get_query_results(self, query_id: str) -> dict:
        """Return the Athena ResultSet (header row first), or raise if not ready."""
        with self._lock:
            record = self._queries.get(query_id)
            if record is None:
                raise KeyError(query_id)
            if record["State"] != "SUCCEEDED":
                raise RuntimeError(
                    f"query {query_id} is {record['State']}, not SUCCEEDED"
                )
            columns, types, rows = (
                record["columns"],
                record["types"],
                record["rows"],
            )
        header = {"Data": [{"VarCharValue": c} for c in columns]}
        data_rows = [
            {"Data": [{} if v is None else {"VarCharValue": str(v)} for v in row]}
            for row in rows
        ]
        column_info = [
            {"Name": name, "Label": name, "Type": _athena_type(typ)}
            for name, typ in zip(columns, types or [""] * len(columns))
        ]
        return {
            "ResultSet": {
                "Rows": [header, *data_rows],
                "ResultSetMetadata": {"ColumnInfo": column_info},
            }
        }

    def stop_query_execution(self, query_id: str) -> None:
        """Mark a query cancelled (best-effort; Trino runs to completion)."""
        with self._lock:
            record = self._queries.get(query_id)
            if record and record["State"] in ("QUEUED", "RUNNING"):
                record["State"] = "CANCELLED"
                record["CompletionDateTime"] = _now()


def _ensure_bucket(uri: str) -> None:
    """Create the default results bucket on first use."""
    bucket, _ = _split_uri(uri)
    s3 = _s3_client()
    try:
        s3.head_bucket(Bucket=bucket)
    except s3.exceptions.ClientError:
        s3.create_bucket(Bucket=bucket)


def _athena_type(trino_type: str) -> str:
    """Map a Trino column type to the nearest Athena type name."""
    base = (trino_type or "varchar").split("(")[0].lower()
    return {
        "integer": "integer",
        "bigint": "bigint",
        "double": "double",
        "real": "float",
        "boolean": "boolean",
        "date": "date",
        "timestamp": "timestamp",
    }.get(base, "varchar")


def _json_response(payload: dict, status: int = 200) -> Response:
    """Return a JSON response, with datetimes as epoch seconds."""

    def default(obj):
        """Serialize datetimes as epoch seconds for json.dumps."""
        if isinstance(obj, datetime.datetime):
            return obj.timestamp()
        raise TypeError

    return Response(
        json.dumps(payload, default=default), status_code=status, media_type=_JSON
    )


def _error(code: str, message: str, status: int = 400) -> Response:
    """Return an Athena JSON error response with its X-Amzn-Errortype."""
    return Response(
        json.dumps({"__type": code, "message": message}),
        status_code=status,
        media_type=_JSON,
        headers={"X-Amzn-Errortype": code},
    )


class AthenaApp:
    """Dispatches Athena operations by X-Amz-Target to the executor."""

    def __init__(self, executor: AthenaExecutor):
        """Bind the dispatcher to an executor."""
        self.executor = executor

    async def handle(self, request: Request) -> Response:
        """Dispatch one Athena request by its X-Amz-Target operation."""
        op = request.headers.get("x-amz-target", "").split(".")[-1]
        body = await request.body()
        try:
            req = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _error("SerializationException", "invalid JSON body")
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            return _error("InvalidRequestException", f"unknown op {op!r}")
        try:
            return handler(req)
        except WorkGroupError as err:
            return _error("InvalidRequestException", str(err))
        except KeyError as err:
            return _error("InvalidRequestException", f"query {err} not found")
        except Exception as err:
            return _error("InvalidRequestException", str(err))

    def op_StartQueryExecution(self, req: dict) -> Response:
        """Start a query; return its id."""
        if not req.get("QueryString"):
            return _error("InvalidRequestException", "QueryString is required")
        return _json_response(
            {"QueryExecutionId": self.executor.start_query_execution(req)}
        )

    def op_GetQueryExecution(self, req: dict) -> Response:
        """Return a query's execution status."""
        result = self.executor.get_query_execution(req["QueryExecutionId"])
        if result is None:
            raise KeyError(req.get("QueryExecutionId"))
        return _json_response(result)

    def op_GetQueryResults(self, req: dict) -> Response:
        """Return a completed query's ResultSet."""
        return _json_response(self.executor.get_query_results(req["QueryExecutionId"]))

    def op_StopQueryExecution(self, req: dict) -> Response:
        """Cancel a running query."""
        self.executor.stop_query_execution(req["QueryExecutionId"])
        return _json_response({})

    def op_GetWorkGroup(self, req: dict) -> Response:
        """Return a workgroup and its configuration."""
        return _json_response(
            {"WorkGroup": self.executor.workgroups.get(req["WorkGroup"])}
        )

    def op_ListWorkGroups(self, req: dict) -> Response:
        """Return the workgroup summaries."""
        return _json_response({"WorkGroups": self.executor.workgroups.summaries()})

    def op_CreateWorkGroup(self, req: dict) -> Response:
        """Create a workgroup."""
        self.executor.workgroups.create(req)
        return _json_response({})

    def op_UpdateWorkGroup(self, req: dict) -> Response:
        """Update a workgroup's configuration, description or state."""
        self.executor.workgroups.update(req)
        return _json_response({})

    def op_DeleteWorkGroup(self, req: dict) -> Response:
        """Delete a workgroup."""
        self.executor.workgroups.delete(req["WorkGroup"])
        return _json_response({})


def create_app(executor: AthenaExecutor | None = None) -> Starlette:
    """Create the Starlette app for the local Athena."""
    dispatcher = AthenaApp(executor or AthenaExecutor())
    return Starlette(routes=[Route("/", dispatcher.handle, methods=["POST"])])


app = create_app()
