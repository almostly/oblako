"""Redshift Data API executor: runs SQL against the oblako/redshift container.

Shapes results like the real ``redshift-data`` API (Field / ColumnMetadata).
Ported from the aws-samples LocalStack Redshift provider, but backed by the
single local oblako/redshift container instead of a per-cluster Postgres server.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import decimal
import re
import threading
import time
import uuid
from typing import Any

import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_IDLE as _IDLE

# Redshift Data API uses named params like `:name`; psycopg2 wants `%(name)s`.
# Match `:name` but not `::cast`.
_NAMED_PARAM = re.compile(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)")


class RedshiftDataExecutor:
    """Executes SQL against the Redshift engine and stores statement results in memory."""

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
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._oid_to_typename: dict[int, str] | None = None

    # -------------------------------------------------------------------------------
    # Connections
    # -------------------------------------------------------------------------------
    def _connect(self, database: str | None = None, cluster: str | None = None):
        """Connect to the shared engine, or to a multi-node cluster's own leader."""
        from oblako.engines.redshift_control import clusters

        record = clusters.get(cluster) if cluster else None
        if record is not None and record.get("status") == "available":
            conn = psycopg2.connect(
                host="localhost",
                port=record["port"],
                user=record["user"],
                password=record["password"],
                dbname=database or record["database"],
            )
        else:
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
    def _record(
        self,
        sql: str,
        database: str | None,
        cluster_identifier: str | None,
        workgroup_name: str | None,
        session_id: str | None,
    ) -> dict:
        """Return a new statement record, not yet run."""
        now = datetime.datetime.now(datetime.timezone.utc)
        return {
            "Id": str(uuid.uuid4()),
            "QueryString": sql,
            "Database": database or self.database,
            "ClusterIdentifier": cluster_identifier,
            **({"WorkgroupName": workgroup_name} if workgroup_name else {}),
            **({"SessionId": session_id} if session_id else {}),
            "CreatedAt": now,
            "UpdatedAt": now,
            "Status": "STARTED",
            "HasResultSet": False,
            "ResultRows": -1,
            "_records": [],
            "_columns": [],
        }

    def _run(self, conn, statement: dict, parameters: list[dict] | None) -> None:
        """Run the statement on ``conn`` and record its result or its error."""
        start = datetime.datetime.now()
        try:
            bound_sql, params = self._bind_params(statement["QueryString"], parameters)
            with conn.cursor() as cur:
                cur.execute(bound_sql, params)
                if cur.description:
                    columns = self._column_metadata(conn, cur.description)
                    records = [
                        [self._encode_field(v) for v in row] for row in cur.fetchall()
                    ]
                    statement["_columns"] = columns
                    statement["_records"] = records
                    statement["HasResultSet"] = True
                    statement["ResultRows"] = len(records)
                else:
                    statement["ResultRows"] = cur.rowcount
            statement["Status"] = "FINISHED"
        except Exception as err:  # SQL errors, parameter binding
            statement["Status"] = "FAILED"
            statement["Error"] = str(err).strip()
        statement["UpdatedAt"] = datetime.datetime.now(datetime.timezone.utc)
        statement["Duration"] = int(
            (datetime.datetime.now() - start).total_seconds() * 1e9
        )  # nanoseconds, like the real API

    def _store(self, statement: dict) -> str:
        """Keep the statement for DescribeStatement and GetStatementResult."""
        with self._lock:
            self._statements[statement["Id"]] = statement
        return statement["Id"]

    def execute(
        self,
        sql: str,
        database: str | None = None,
        cluster_identifier: str | None = None,
        parameters: list[dict] | None = None,
        workgroup_name: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Run SQL, store the statement + result, return the statement id.

        A Serverless workgroup is the shared engine, so ``workgroup_name`` only
        labels the statement. With ``session_id`` the statement runs on that
        session's connection, so it shares the session's transaction.
        """
        session = self.session(session_id) if session_id else None
        if session is not None:
            database, cluster_identifier = session["database"], session["cluster"]
            workgroup_name = session["workgroup"]
        statement = self._record(
            sql, database, cluster_identifier, workgroup_name, session_id
        )
        if session is not None:
            with session["lock"]:
                self._run(session["conn"], statement, parameters)
                self._touch(session)
            return self._store(statement)
        try:
            conn = self._connect(database, cluster_identifier)
        except Exception as err:  # connection errors
            statement["Status"] = "FAILED"
            statement["Error"] = str(err).strip()
            return self._store(statement)
        try:
            self._run(conn, statement, parameters)
        finally:
            conn.close()
        return self._store(statement)

    def execute_batch(
        self,
        sqls: list[str],
        database: str | None = None,
        cluster_identifier: str | None = None,
        workgroup_name: str | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        """Run the statements in order as one transaction; return their ids.

        As on AWS: a statement that fails rolls the batch back, and the ones after
        it are ABORTED, never run. In a session that already has a transaction
        open, the batch runs inside it and leaves the outcome to the session.
        """
        session = self.session(session_id) if session_id else None
        if session is not None:
            database, cluster_identifier = session["database"], session["cluster"]
            workgroup_name = session["workgroup"]
        statements = [
            self._record(sql, database, cluster_identifier, workgroup_name, session_id)
            for sql in sqls
        ]
        conn = (
            session["conn"]
            if session is not None
            else self._connect(database, cluster_identifier)
        )
        lock = session["lock"] if session is not None else threading.Lock()
        try:
            with lock:
                own = conn.info.transaction_status == _IDLE
                if own:
                    conn.cursor().execute("BEGIN")
                failed = False
                for statement in statements:
                    if failed:
                        statement["Status"] = "ABORTED"
                        continue
                    self._run(conn, statement, None)
                    failed = statement["Status"] == "FAILED"
                if own:
                    conn.cursor().execute("ROLLBACK" if failed else "COMMIT")
                if session is not None:
                    self._touch(session)
        finally:
            if session is None:
                conn.close()
        return [self._store(statement) for statement in statements]

    # -------------------------------------------------------------------------------
    # Sessions
    # -------------------------------------------------------------------------------
    def open_session(
        self,
        keep_alive: int,
        database: str | None = None,
        cluster_identifier: str | None = None,
        workgroup_name: str | None = None,
    ) -> str:
        """Open a session: one engine connection its statements share; return its id."""
        self._expire_sessions()
        session_id = str(uuid.uuid4())
        session = {
            "conn": self._connect(database, cluster_identifier),
            "database": database or self.database,
            "cluster": cluster_identifier,
            "workgroup": workgroup_name,
            "keep_alive": keep_alive,
            "lock": threading.Lock(),
        }
        self._touch(session)
        with self._lock:
            self._sessions[session_id] = session
        return session_id

    def session(self, session_id: str) -> dict:
        """Return an open session, or raise KeyError for one unknown or expired."""
        self._expire_sessions()
        with self._lock:
            return self._sessions[session_id]

    @staticmethod
    def _touch(session: dict) -> None:
        """Keep the session alive for its keep-alive seconds from now."""
        session["expires"] = time.monotonic() + session["keep_alive"]

    def _expire_sessions(self) -> None:
        """Close the sessions idle past their keep-alive (an open transaction rolls back)."""
        now = time.monotonic()
        with self._lock:
            gone = [
                sid
                for sid, s in self._sessions.items()
                if s["expires"] < now and not s["lock"].locked()
            ]
            for sid in gone:
                with contextlib.suppress(Exception):
                    self._sessions[sid]["conn"].close()
                del self._sessions[sid]

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
    def _scalar_list(
        self, sql: str, database: str | None = None, cluster: str | None = None
    ) -> list[str]:
        """Run ``sql`` and return the first column of every row."""
        conn = self._connect(database, cluster)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    def list_databases(
        self, database: str | None = None, cluster: str | None = None
    ) -> list[str]:
        """Return the names of all non-template databases."""
        return self._scalar_list(
            "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname",
            database,
            cluster,
        )

    def list_schemas(
        self, database: str | None = None, cluster: str | None = None
    ) -> list[str]:
        """Return the names of all schemas in the specified database."""
        return self._scalar_list(
            "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name",
            database,
            cluster,
        )

    def list_tables(
        self,
        database: str | None = None,
        schema_pattern: str | None = None,
        table_pattern: str | None = None,
        cluster: str | None = None,
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
        conn = self._connect(database, cluster)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or None)
                return [
                    {"name": name, "schema": schema, "type": ttype}
                    for name, schema, ttype in cur.fetchall()
                ]
        finally:
            conn.close()

    def describe_table(
        self,
        table: str,
        database: str | None = None,
        schema: str | None = None,
        cluster: str | None = None,
    ) -> list[dict]:
        """Return column metadata for the named table.

        ``typeName`` is the internal catalog type name (``int4``, ``float4``,
        ``timestamp``, ``varchar``, ...), exactly what real Redshift's
        ``DescribeTable`` reports, not the ``information_schema`` spelling
        (``integer``, ``real``, ...). Clients such as Feast key their Redshift
        type map on the internal names, so returning ``integer`` breaks them.
        """
        conn = self._connect(database, cluster)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT a.attname,
                           t.typname,
                           CASE WHEN a.atttypmod > 4 THEN a.atttypmod - 4 ELSE 0 END,
                           a.attnotnull,
                           pg_catalog.pg_get_expr(d.adbin, d.adrelid)
                    FROM pg_catalog.pg_attribute a
                    JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
                    LEFT JOIN pg_catalog.pg_attrdef d
                           ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                    WHERE c.relname = %s
                      AND a.attnum > 0 AND NOT a.attisdropped
                      AND (%s::text IS NULL OR n.nspname = %s)
                    ORDER BY a.attnum
                    """,
                    (table, schema, schema),
                )
                return [
                    {
                        "name": name,
                        "typeName": typname,
                        "length": length or 0,
                        "nullable": 0 if notnull else 1,
                        "columnDefault": default,
                        "tableName": table,
                    }
                    for name, typname, length, notnull, default in cur.fetchall()
                ]
        finally:
            conn.close()


def _to_pg_array(values) -> str:
    """Convert to pg array helper."""

    def fmt(element):
        """Return one element as a PostgreSQL array literal item, quoted if needed."""
        if element is None:
            return "NULL"
        text = str(element)
        if any(c in text for c in ',"{} ') or not text or text.lower() == "null":
            return '"' + text.replace('"', '""') + '"'
        return text

    return "{" + ",".join(fmt(v) for v in values) + "}"
