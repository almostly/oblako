"""RDS Data API executor: synchronous SQL over the RDS engine (Postgres or MySQL).

Unlike redshift-data (async), rds-data's ExecuteStatement returns results
immediately, supports transactions, and takes Field-valued parameters. The
engine differs only in the driver (psycopg2 / pymysql), the autocommit API, and
type-name resolution; Field encoding, `%(name)s` binding, and transaction logic
are shared. Field/array encoding is reused from redshift_data.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import json
import re
import threading
import uuid

from oblako.redshift_data.executor import RedshiftDataExecutor

_encode_field = RedshiftDataExecutor._encode_field  # generic PG/py value -> Field
_NAMED_PARAM = re.compile(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)")


def _json_safe(value):
    """Plain JSON value for formattedRecords."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return str(value)


class RdsDataExecutor:
    """Synchronous SQL executor for the RDS Data API (Postgres or MySQL)."""

    def __init__(self, host="localhost", port=5432, user="oblako", password="oblako",
                 database="oblako", engine="postgres"):
        """Initialize connection parameters and choose the driver for the given engine."""
        if engine not in ("postgres", "mysql"):
            raise ValueError(f"engine must be 'postgres' or 'mysql', got {engine!r}")
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.engine = engine
        self._txns: dict[str, object] = {}
        self._lock = threading.Lock()
        self._pg_type_cache: dict[int, str] | None = None
        self._mysql_type_cache: dict[int, str] | None = None

    # -- connections / metadata --------------------------------------------
    def _connect(self, database=None, autocommit=True):
        db = database or self.database
        if self.engine == "mysql":
            try:
                import pymysql
            except ImportError as e:
                raise ImportError(
                    "rds-data over MySQL needs pymysql: pip install 'oblako[mysql]'"
                ) from e
            conn = pymysql.connect(host=self.host, port=self.port, user=self.user,
                                   password=self.password, database=db)
            conn.autocommit(autocommit)  # pymysql: method
            return conn
        import psycopg2

        conn = psycopg2.connect(host=self.host, port=self.port, user=self.user,
                                password=self.password, dbname=db)
        conn.autocommit = autocommit  # psycopg2: attribute
        return conn

    def _pg_types(self, conn) -> dict[int, str]:
        if self._pg_type_cache is None:
            with conn.cursor() as cur:
                cur.execute("SELECT oid, typname FROM pg_type")
                self._pg_type_cache = {oid: name for oid, name in cur.fetchall()}
        return self._pg_type_cache

    def _mysql_types(self) -> dict[int, str]:
        if self._mysql_type_cache is None:
            from pymysql.constants import FIELD_TYPE

            self._mysql_type_cache = {
                v: k for k, v in vars(FIELD_TYPE).items()
                if isinstance(v, int) and not k.startswith("_")
            }
        return self._mysql_type_cache

    def _typename(self, type_code, conn) -> str:
        if self.engine == "mysql":
            return self._mysql_types().get(type_code, "unknown")
        return self._pg_types(conn).get(type_code, "unknown")

    def _column_metadata(self, conn, description) -> list[dict]:
        # psycopg2 Column namedtuples and pymysql tuples both index positionally:
        # (name, type_code, display_size, internal_size, precision, scale, null_ok)
        cols = []
        for c in description:
            cols.append({
                "name": c[0],
                "label": c[0],
                "typeName": self._typename(c[1], conn),
                "nullable": 1,
                "precision": (c[4] if len(c) > 4 else 0) or 0,
                "scale": (c[5] if len(c) > 5 else 0) or 0,
            })
        return cols

    # -- parameters (Field-valued) -----------------------------------------
    @staticmethod
    def _param_scalar(field: dict):
        if not field or field.get("isNull"):
            return None
        if "blobValue" in field:
            return base64.b64decode(field["blobValue"])
        if "arrayValue" in field:
            return json.dumps(field["arrayValue"])
        return next(iter(field.values()))

    def _bind(self, sql: str, parameters):
        if not parameters:
            return sql, None
        named = {p["name"]: self._param_scalar(p.get("value", {})) for p in parameters}
        return _NAMED_PARAM.sub(r"%(\1)s", sql), named

    # -- statements --------------------------------------------------------
    def execute(self, sql, database=None, parameters=None, transaction_id=None,
                include_result_metadata=False, format_records_as=None) -> dict:
        """Execute a SQL statement and return the result dict."""
        bound, params = self._bind(sql, parameters)
        if transaction_id:
            conn = self._txns.get(transaction_id)
            if conn is None:
                raise ValueError(f"Transaction {transaction_id} is not found")
            temp = False
        else:
            conn = self._connect(database)
            temp = True
        try:
            with conn.cursor() as cur:
                cur.execute(bound, params)
                out: dict = {"numberOfRecordsUpdated": 0, "generatedFields": []}
                if cur.description:
                    rows = cur.fetchall()
                    if format_records_as == "JSON":
                        cols = [c[0] for c in cur.description]
                        out["formattedRecords"] = json.dumps(
                            [{c: _json_safe(v) for c, v in zip(cols, row)} for row in rows]
                        )
                    else:
                        out["records"] = [[_encode_field(v) for v in row] for row in rows]
                    if include_result_metadata:
                        out["columnMetadata"] = self._column_metadata(conn, cur.description)
                else:
                    out["numberOfRecordsUpdated"] = max(cur.rowcount, 0)
                return out
        finally:
            if temp:
                conn.close()

    def batch(self, sql, parameter_sets=None, transaction_id=None) -> list[dict]:
        """Execute a parameterised statement once per parameter set and return update results."""
        sets = parameter_sets if parameter_sets else [None]
        if transaction_id:
            conn = self._txns.get(transaction_id)
            if conn is None:
                raise ValueError(f"Transaction {transaction_id} is not found")
            temp = False
        else:
            conn = self._connect()
            temp = True
        try:
            with conn.cursor() as cur:
                results = []
                for ps in sets:
                    bound, params = self._bind(sql, ps)
                    cur.execute(bound, params)
                    results.append({"generatedFields": []})
                return results
        finally:
            if temp:
                conn.close()

    # -- transactions ------------------------------------------------------
    def begin(self, database=None) -> str:
        """Open a new database transaction and return its id."""
        tid = uuid.uuid4().hex
        conn = self._connect(database, autocommit=False)
        with self._lock:
            self._txns[tid] = conn
        return tid

    def _pop(self, transaction_id):
        with self._lock:
            conn = self._txns.pop(transaction_id, None)
        if conn is None:
            raise ValueError(f"Transaction {transaction_id} is not found")
        return conn

    def commit(self, transaction_id) -> str:
        """Commit the given transaction and close its connection."""
        conn = self._pop(transaction_id)
        try:
            conn.commit()
        finally:
            conn.close()
        return "Transaction Committed"

    def rollback(self, transaction_id) -> str:
        """Roll back the given transaction and close its connection."""
        conn = self._pop(transaction_id)
        try:
            conn.rollback()
        finally:
            conn.close()
        return "Rollback Complete"
