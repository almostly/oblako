"""ASGI app implementing the AWS `rds-data` (RDS Data API) wire protocol.

rest-json with per-operation routes. boto3's ``rds-data`` client talks to it via
``endpoint_url``; statements run synchronously against the RDS Postgres engine.

    POST /Execute              -> ExecuteStatement
    POST /BatchExecute         -> BatchExecuteStatement
    POST /BeginTransaction     -> BeginTransaction
    POST /CommitTransaction    -> CommitTransaction
    POST /RollbackTransaction  -> RollbackTransaction
"""

from __future__ import annotations

import json
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .executor import RdsDataExecutor


def _error(message: str, error_type: str = "BadRequestException", status: int = 400) -> Response:
    """Return a JSON error response with the given message and error type."""
    return JSONResponse(
        {"message": message, "__type": error_type},
        status_code=status,
        headers={"x-amzn-errortype": error_type},
    )


class RdsDataApp:
    """Handles rds-data HTTP routes and delegates to an RdsDataExecutor."""

    def __init__(self, executor: RdsDataExecutor):
        """Bind the app to the given executor."""
        self.executor = executor

    async def _body(self, request: Request) -> dict:
        """Parse and return the JSON request body."""
        raw = await request.body()
        return json.loads(raw) if raw else {}

    async def execute(self, request: Request) -> Response:
        """Handle ExecuteStatement: run a SQL statement and return the result."""
        req = await self._body(request)
        if not req.get("sql"):
            return _error("SQL is required")
        try:
            result = self.executor.execute(
                sql=req["sql"],
                database=req.get("database"),
                parameters=req.get("parameters"),
                transaction_id=req.get("transactionId"),
                include_result_metadata=req.get("includeResultMetadata", False),
                format_records_as=req.get("formatRecordsAs"),
            )
        except Exception as e:  # noqa: BLE001 - any driver/SQL error -> BadRequest
            return _error(str(e).strip())
        return JSONResponse(result)

    async def batch_execute(self, request: Request) -> Response:
        """Handle BatchExecuteStatement: run a parameterised statement for each parameter set."""
        req = await self._body(request)
        if not req.get("sql"):
            return _error("SQL is required")
        try:
            results = self.executor.batch(
                sql=req["sql"],
                parameter_sets=req.get("parameterSets"),
                transaction_id=req.get("transactionId"),
            )
        except Exception as e:  # noqa: BLE001
            return _error(str(e).strip())
        return JSONResponse({"updateResults": results})

    async def begin(self, request: Request) -> Response:
        """Handle BeginTransaction: open a new transaction and return its id."""
        req = await self._body(request)
        try:
            tid = self.executor.begin(database=req.get("database"))
        except Exception as e:  # noqa: BLE001
            return _error(str(e).strip())
        return JSONResponse({"transactionId": tid})

    async def commit(self, request: Request) -> Response:
        """Handle CommitTransaction: commit and close the given transaction."""
        req = await self._body(request)
        try:
            status = self.executor.commit(req["transactionId"])
        except Exception as e:  # noqa: BLE001
            return _error(str(e).strip())
        return JSONResponse({"transactionStatus": status})

    async def rollback(self, request: Request) -> Response:
        """Handle RollbackTransaction: roll back and close the given transaction."""
        req = await self._body(request)
        try:
            status = self.executor.rollback(req["transactionId"])
        except Exception as e:  # noqa: BLE001
            return _error(str(e).strip())
        return JSONResponse({"transactionStatus": status})


def create_app(executor: RdsDataExecutor | None = None) -> Starlette:
    """Create and return the Starlette ASGI application for the rds-data service."""
    engine = os.environ.get("OBLAKO_RDS_ENGINE", "postgres")
    executor = executor or RdsDataExecutor(
        host=os.environ.get("OBLAKO_RDS_HOST", "localhost"),
        port=int(os.environ.get("OBLAKO_RDS_PORT", "3306" if engine == "mysql" else "5432")),
        user=os.environ.get("OBLAKO_RDS_USER", "oblako"),
        password=os.environ.get("OBLAKO_RDS_PASSWORD", "oblako"),
        database=os.environ.get("OBLAKO_RDS_DB", "oblako"),
        engine=engine,
    )
    h = RdsDataApp(executor)

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/", health, methods=["GET"]),
            Route("/Execute", h.execute, methods=["POST"]),
            Route("/BatchExecute", h.batch_execute, methods=["POST"]),
            Route("/BeginTransaction", h.begin, methods=["POST"]),
            Route("/CommitTransaction", h.commit, methods=["POST"]),
            Route("/RollbackTransaction", h.rollback, methods=["POST"]),
        ]
    )


# Module-level app for `uvicorn oblako.rds_data.app:app`
app = create_app()
