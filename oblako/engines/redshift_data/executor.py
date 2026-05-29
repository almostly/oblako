"""Redshift Data API executor: runs SQL against the pgredshift container.

Shapes results like the real ``redshift-data`` API (Field / ColumnMetadata).
Ported from the aws-samples LocalStack Redshift provider, but backed by the
single local pgredshift container instead of a per-cluster Postgres server.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import re
import threading
import uuid
from typing import Any

import psycopg2

# Redshift Data API uses named params like `:name`; psycopg2 wants `%(name)s`.
# Match `:name` but not `::cast`.
_NAMED_PARAM = re.compile(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)")


class RedshiftDataExecutor:
    """Executes SQL against pgredshift and stores statement results in memory."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5439,
        user: str = "oblako",
        password: str = "oblako",
        database: str = "oblako",
    ):
        """Initialize connection parameters and empty in-memory statement store."""
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self._statements: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._oid_to_typename: dict[int, str] | None = None

    # -------------------------------------------------------------------------------
    # Connections
    # -------------------------------------------------------------------------------
    def _connect(self, database: str | None = None):
        conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=database or self.database,
        )
        conn.autocommit = True
        return conn

    def _type_map(self, conn) -> dict[int, str]:
        """Cache the pg type OID -> type name map (stable per database)."""
        if self._oid_to_typename is None:
            with conn.cursor() as cur:
                cur.execute("SELECT oid, typname FROM pg_type")
                self._oid_to_typename = {oid: name for oid, name in cur.fetchall()}
        return self._oid_to_typename

    # -------------------------------------------------------------------------------
    # Field column-encoding
    # -------------------------------------------------------------------------------
    @staticmethod
    def _encode_field(value: Any) -> dict:
        """Encode a Python value as a redshift-data Field union member."""
        if value is None:
            return {"isNull": True}
        if isinstance(value, bool):
            return {"booleanValue": value}
        if isinstance(value, int):
            return {"longValue": value}
        if isinstance(value, float):
            return {"doubleValue": value}
        if isinstance(value, decimal.Decimal):
            # preserve precision the way the real API does for NUMERIC
            return {"stringValue": str(value)}
        if isinstance(value, (bytes, memoryview)):
            return {"blobValue": base64.b64encode(bytes(value)).decode("ascii")}
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return {"stringValue": value.isoformat()}
        if isinstance(value, (list, tuple)):
            return {"stringValue": _to_pg_array(value)}
        if isinstance(value, dict):
            import json

            return {"stringValue": json.dumps(value)}
        return {"stringValue": str(value)}

    def _column_metadata(self, conn, description) -> list[dict]:
        """Build a ColumnMetadata list from a cursor description."""
        type_map = self._type_map(conn)
        columns = []
        for col in description:
            columns.append(
                {
                    "name": col.name,
                    "label": col.name,
                    "typeName": type_map.get(col.type_code, "unknown"),
                    "nullable": 1,
                    "length": col.internal_size
                    if col.internal_size and col.internal_size > 0
                    else 0,
                    "precision": col.precision or 0,
                    "scale": col.scale or 0,
                }
            )
        return columns

    @staticmethod
    def _bind_params(sql: str, parameters: list[dict] | None):
        """Rewrite named ``:param`` placeholders to ``%(param)s`` and build a value dict."""
        if not parameters:
            return sql, None
        named = {p["name"]: p["value"] for p in parameters}
        rewritten = _NAMED_PARAM.sub(r"%(\1)s", sql)
        return rewritten, named

    # -------------------------------------------------------------------------------
    # Statement execution
    # -------------------------------------------------------------------------------
    def execute(
        self,
        sql: str,
        database: str | None = None,
        cluster_identifier: str | None = None,
        parameters: list[dict] | None = None,
    ) -> str:
        """Run SQL, store the statement + result, return the statement id."""
        stmt_id = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc)
        statement = {
            "Id": stmt_id,
            "QueryString": sql,
            "Database": database or self.database,
            "ClusterIdentifier": cluster_identifier,
            "CreatedAt": now,
            "UpdatedAt": now,
            "Status": "STARTED",
            "HasResultSet": False,
            "ResultRows": -1,
            "_records": [],
            "_columns": [],
        }
        start = datetime.datetime.now()
        try:
            from oblako.engines import redshift_ml

            if redshift_ml.is_create_model(sql):
                # Redshift ML: train via SageMaker local + create an in-DB predict UDF.
                summary = redshift_ml.create_model(
                    redshift_ml.parse_create_model(sql),
                    host=self.host,
                    port=self.port,
                    user=self.user,
                    password=self.password,
                    database=database or self.database,
                )
                statement["ModelSummary"] = summary
                statement["ResultRows"] = 0
                statement["Status"] = "FINISHED"
                statement["UpdatedAt"] = datetime.datetime.now(datetime.timezone.utc)
                statement["Duration"] = int(
                    (datetime.datetime.now() - start).total_seconds() * 1e9
                )
                with self._lock:
                    self._statements[stmt_id] = statement
                return stmt_id

            bound_sql, params = self._bind_params(sql, parameters)
            conn = self._connect(database)
            try:
                with conn.cursor() as cur:
                    cur.execute(bound_sql, params)
                    if cur.description:
                        columns = self._column_metadata(conn, cur.description)
                        records = [
                            [self._encode_field(v) for v in row]
                            for row in cur.fetchall()
                        ]
                        statement["_columns"] = columns
                        statement["_records"] = records
                        statement["HasResultSet"] = True
                        statement["ResultRows"] = len(records)
                    else:
                        statement["ResultRows"] = cur.rowcount
            finally:
                conn.close()
            statement["Status"] = "FINISHED"
        except psycopg2.Error as err:
            statement["Status"] = "FAILED"
            statement["Error"] = str(err).strip()
        except Exception as err:  # noqa: BLE001 - CREATE MODEL parse/training errors
            statement["Status"] = "FAILED"
            statement["Error"] = str(err).strip()
        statement["UpdatedAt"] = datetime.datetime.now(datetime.timezone.utc)
        statement["Duration"] = int(
            (datetime.datetime.now() - start).total_seconds() * 1e9
        )  # nanoseconds, like the real API
        with self._lock:
            self._statements[stmt_id] = statement
        return stmt_id

    def get(self, stmt_id: str) -> dict | None:
        """Return the raw statement record, or None if not found."""
        with self._lock:
            return self._statements.get(stmt_id)

    def describe(self, stmt_id: str) -> dict | None:
        """Return public metadata for a statement, omitting internal ``_``-prefixed keys."""
        stmt = self.get(stmt_id)
        if not stmt:
            return None
        return {k: v for k, v in stmt.items() if not k.startswith("_")}

    def result(self, stmt_id: str) -> dict | None:
        """Return the ColumnMetadata and Records for a completed statement."""
        stmt = self.get(stmt_id)
        if not stmt:
            return None
        return {
            "ColumnMetadata": stmt["_columns"],
            "Records": stmt["_records"],
            "TotalNumRows": len(stmt["_records"]),
        }

    def list_statements(self) -> list[dict]:
        """Return a summary list of all stored statements."""
        with self._lock:
            return [
                {
                    "Id": s["Id"],
                    "QueryString": s["QueryString"],
                    "Status": s["Status"],
                    "CreatedAt": s["CreatedAt"],
                    "UpdatedAt": s["UpdatedAt"],
                }
                for s in self._statements.values()
            ]

    # -------------------------------------------------------------------------------
    # Catalog helpers
    # -------------------------------------------------------------------------------
    def _scalar_list(self, sql: str, database: str | None = None) -> list[str]:
        conn = self._connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def list_databases(self, database: str | None = None) -> list[str]:
        """Return the names of all non-template databases."""
        return self._scalar_list(
            "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname",
            database,
        )

    def list_schemas(self, database: str | None = None) -> list[str]:
        """Return the names of all schemas in the specified database."""
        return self._scalar_list(
            "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name",
            database,
        )

    def list_tables(
        self,
        database: str | None = None,
        schema_pattern: str | None = None,
        table_pattern: str | None = None,
    ) -> list[dict]:
        """Return tables matching optional schema and name LIKE patterns."""
        clauses = ["table_schema NOT IN ('pg_catalog', 'information_schema')"]
        params: list[str] = []
        if schema_pattern:
            clauses.append("table_schema LIKE %s")
            params.append(schema_pattern)
        if table_pattern:
            clauses.append("table_name LIKE %s")
            params.append(table_pattern)
        sql = (
            "SELECT table_name, table_schema, table_type FROM information_schema.tables "
            f"WHERE {' AND '.join(clauses)} ORDER BY table_schema, table_name"
        )
        conn = self._connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or None)
                return [
                    {"name": name, "schema": schema, "type": ttype}
                    for name, schema, ttype in cur.fetchall()
                ]
        finally:
            conn.close()

    def describe_table(self, table: str, database: str | None = None) -> list[dict]:
        """Return column metadata for the named table."""
        conn = self._connect(database)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name, data_type, character_maximum_length,
                           is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (table,),
                )
                return [
                    {
                        "name": name,
                        "typeName": data_type,
                        "length": length or 0,
                        "nullable": 1 if is_nullable == "YES" else 0,
                        "columnDefault": default,
                        "tableName": table,
                    }
                    for name, data_type, length, is_nullable, default in cur.fetchall()
                ]
        finally:
            conn.close()


def _to_pg_array(values) -> str:
    """Convert to pg array helper."""

    def fmt(element):
        if element is None:
            return "NULL"
        text = str(element)
        if any(c in text for c in ',"{} ') or not text or text.lower() == "null":
            return '"' + text.replace('"', '""') + '"'
        return text

    return "{" + ",".join(fmt(v) for v in values) + "}"
