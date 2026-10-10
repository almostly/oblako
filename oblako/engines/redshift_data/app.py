"""ASGI app implementing the AWS `redshift-data` wire protocol (JSON 1.1).

boto3's ``redshift-data`` client can talk to this directly via ``endpoint_url``.
Each request is a POST with header ``X-Amz-Target: RedshiftData.<Operation>``
and a JSON body. Statements execute against the local oblako/redshift container.
"""

from __future__ import annotations

import datetime
import json
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from oblako.engines.redshift_control import serverless

from .executor import RedshiftDataExecutor

# BatchExecuteStatement takes at most this many statements (Sqls), as on AWS
MAX_BATCH_SQLS = 40


def _jsonable(obj):
    """JSON-encode, converting datetimes to unix epoch (json protocol timestamps)."""
    if isinstance(obj, datetime.datetime):
        return obj.timestamp()
    raise TypeError(f"not JSON serializable: {type(obj)}")


def _json_response(
    payload: dict, status: int = 200, error_type: str | None = None
) -> Response:
    """Return an ``application/x-amz-json-1.1`` response."""
    headers = {"Content-Type": "application/x-amz-json-1.1"}
    if error_type:
        headers["X-Amzn-Errortype"] = error_type
    return Response(
        json.dumps(payload, default=_jsonable), status_code=status, headers=headers
    )


def _error(code: str, message: str, status: int = 400) -> Response:
    """Return a JSON error response with the given error code and message."""
    return _json_response(
        {"__type": code, "message": message}, status=status, error_type=code
    )


class RedshiftDataApp:
    """Dispatches redshift-data operations to a RedshiftDataExecutor."""

    def __init__(self, executor: RedshiftDataExecutor):
        """Bind the dispatcher to the given executor."""
        self.executor = executor

    async def handle(self, request: Request) -> Response:
        """Dispatch an incoming request to the appropriate operation handler."""
        target = request.headers.get("X-Amz-Target", "")
        op = target.split(".")[-1]
        try:
            body = await request.body()
            req = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _error("ValidationException", "Invalid JSON body")

        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            return _error("ValidationException", f"Unknown operation: {op or '(none)'}")
        refused = _check_workgroup(req)
        if refused is not None:
            return refused
        try:
            return handler(req)
        except _NotFound as e:
            return _error("ResourceNotFoundException", str(e))
        except Exception as e:  # surface backend errors as ValidationException
            return _error("ValidationException", str(e))

    # -------------------------------------------------------------------------------
    # Operations
    # -------------------------------------------------------------------------------
    def _stored(self, stmt_id: str) -> dict:
        """Return a statement this server just ran (always stored by execute)."""
        stmt = self.executor.get(stmt_id)
        if stmt is None:
            raise _NotFound(stmt_id)
        return stmt

    def op_ExecuteStatement(self, req: dict) -> Response:
        """Execute a single SQL statement and return its statement id."""
        if not req.get("Sql"):
            return _error("ValidationException", "Sql is required")
        stmt_id = self.executor.execute(
            sql=req["Sql"],
            database=req.get("Database"),
            cluster_identifier=req.get("ClusterIdentifier"),
            parameters=req.get("Parameters"),
            workgroup_name=req.get("WorkgroupName"),
        )
        stmt = self._stored(stmt_id)
        return _json_response(
            {
                "Id": stmt_id,
                **_target(req),
                "Database": stmt["Database"],
                "CreatedAt": stmt["CreatedAt"],
            }
        )

    def op_BatchExecuteStatement(self, req: dict) -> Response:
        """Execute multiple SQL statements in sequence and return the last statement id."""
        sqls = req.get("Sqls") or []
        if not sqls:
            return _error("ValidationException", "Sqls is required")
        if len(sqls) > MAX_BATCH_SQLS:
            return _error(
                "ValidationException",
                f"1 validation error detected: Value '[{', '.join(sqls)}]' at 'sqls' "
                "failed to satisfy constraint: Member must have length less than or "
                f"equal to {MAX_BATCH_SQLS}",
            )
        ids = [
            self.executor.execute(
                sql=sql,
                database=req.get("Database"),
                cluster_identifier=req.get("ClusterIdentifier"),
                parameters=req.get("Parameters"),
                workgroup_name=req.get("WorkgroupName"),
            )
            for sql in sqls
        ]
        last = self._stored(ids[-1])
        # link the sub-statements so DescribeStatement can report them
        subs = [self._stored(sid) for sid in ids]
        last["SubStatements"] = [
            {
                "Id": sub["Id"],
                "QueryString": sub["QueryString"],
                "Status": sub["Status"],
                "HasResultSet": sub["HasResultSet"],
                "ResultRows": sub["ResultRows"],
            }
            for sub in subs
        ]
        return _json_response(
            {
                "Id": ids[-1],
                **_target(req),
                "Database": last["Database"],
                "CreatedAt": last["CreatedAt"],
            }
        )

    def op_DescribeStatement(self, req: dict) -> Response:
        """Return metadata for a previously submitted statement."""
        stmt = self.executor.describe(_require(req, "Id"))
        if not stmt:
            raise _NotFound(f"Statement {req.get('Id')} not found")
        return _json_response(stmt)

    def op_GetStatementResult(self, req: dict) -> Response:
        """Return the result set for a completed statement."""
        result = self.executor.result(_require(req, "Id"))
        if result is None:
            raise _NotFound(f"Statement {req.get('Id')} not found")
        return _json_response(result)

    def op_CancelStatement(self, req: dict) -> Response:
        """Acknowledge a cancel request (statements execute synchronously)."""
        _require(req, "Id")
        return _json_response({"Status": True})

    def op_ListStatements(self, req: dict) -> Response:
        """Return a summary list of all submitted statements."""
        return _json_response({"Statements": self.executor.list_statements()})

    def op_ListDatabases(self, req: dict) -> Response:
        """Return the list of databases in the cluster."""
        return _json_response(
            {
                "Databases": self.executor.list_databases(
                    req.get("Database"), req.get("ClusterIdentifier")
                )
            }
        )

    def op_ListSchemas(self, req: dict) -> Response:
        """Return the list of schemas in the specified database."""
        return _json_response(
            {
                "Schemas": self.executor.list_schemas(
                    req.get("Database"), req.get("ClusterIdentifier")
                )
            }
        )

    def op_ListTables(self, req: dict) -> Response:
        """Return tables matching optional schema and name patterns."""
        tables = self.executor.list_tables(
            database=req.get("Database"),
            schema_pattern=req.get("SchemaPattern"),
            table_pattern=req.get("TablePattern"),
            cluster=req.get("ClusterIdentifier"),
        )
        return _json_response({"Tables": tables})

    def op_DescribeTable(self, req: dict) -> Response:
        """Return column metadata for the specified table."""
        table = _require(req, "Table")
        return _json_response(
            {
                "TableName": table,
                "ColumnList": self.executor.describe_table(
                    table,
                    req.get("Database"),
                    req.get("Schema"),
                    req.get("ClusterIdentifier"),
                ),
            }
        )


class _NotFound(Exception):
    pass


def _target(req: dict) -> dict:
    """Return the cluster or the workgroup a statement ran on, as AWS echoes it."""
    if req.get("WorkgroupName"):
        return {"WorkgroupName": req["WorkgroupName"]}
    return {"ClusterIdentifier": req.get("ClusterIdentifier")}


def _check_workgroup(req: dict) -> Response | None:
    """Refuse an unknown workgroup, or a request naming a cluster and a workgroup."""
    name = req.get("WorkgroupName")
    if not name:
        return None
    if req.get("ClusterIdentifier"):
        return _error(
            "ValidationException",
            "Specify either a ClusterIdentifier or a WorkgroupName, not both",
        )
    if serverless.get_workgroup_record(name) is None:
        return _error("ValidationException", f"Workgroup {name} not found")
    return None


def _require(req: dict, key: str):
    """Return ``req[key]`` or raise if the key is absent or None."""
    if key not in req or req[key] is None:
        raise Exception(f"{key} is required")
    return req[key]


def create_app(executor: RedshiftDataExecutor | None = None) -> Starlette:
    """Create and return the Starlette ASGI application for the redshift-data service."""
    executor = executor or RedshiftDataExecutor(
        host=os.environ.get("OBLAKO_REDSHIFT_HOST", "localhost"),
        port=int(os.environ.get("OBLAKO_REDSHIFT_PORT", "5439")),
        user=os.environ.get("OBLAKO_REDSHIFT_USER", "oblako"),
        password=os.environ.get("OBLAKO_REDSHIFT_PASSWORD", "oblako"),
        database=os.environ.get("OBLAKO_REDSHIFT_DB", "oblako"),
    )
    dispatcher = RedshiftDataApp(executor)
    return Starlette(routes=[Route("/", dispatcher.handle, methods=["POST"])])


# Module-level app for `uvicorn oblako.redshift_data.app:app`
app = create_app()
