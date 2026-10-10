"""The Glue catalog's own tables: databases, tables and partitions in SQLite.

Iceberg tables live in the Iceberg REST catalog; this store holds everything
Glue knows that the REST catalog doesn't: Hive-style tables (Parquet / CSV /
JSON under an S3 location, as awswrangler and Athena CTAS create them) with
their partitions, database metadata, and the full ``TableInput`` of Iceberg
tables created through Glue (PyIceberg's Glue catalog).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path


class GlueStore:
    """Glue catalog state, persisted in SQLite (``~/.oblako/glue/catalog.db``)."""

    def __init__(self, path: str | None = None):
        """Open (and create) the database."""
        path = path or os.environ.get(
            "OBLAKO_GLUE_DB", str(Path.home() / ".oblako" / "glue" / "catalog.db")
        )
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS databases (
                    name TEXT PRIMARY KEY, input TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS tables (
                    db TEXT, name TEXT, input TEXT, created REAL, updated REAL,
                    version INTEGER, PRIMARY KEY (db, name));
                CREATE TABLE IF NOT EXISTS partitions (
                    db TEXT, tbl TEXT, vals TEXT, input TEXT, created REAL,
                    PRIMARY KEY (db, tbl, vals));
                CREATE TABLE IF NOT EXISTS column_stats (
                    db TEXT, tbl TEXT, vals TEXT, col TEXT, stats TEXT,
                    PRIMARY KEY (db, tbl, vals, col));
                CREATE TABLE IF NOT EXISTS connections (
                    name TEXT PRIMARY KEY, input TEXT, created REAL, updated REAL);
                """
            )

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        """Run one SQL statement, commit, and return its rows."""
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            self._db.commit()
            return rows

    # -------------------------------------------------------------------------
    # Databases
    # -------------------------------------------------------------------------
    def database(self, name: str) -> dict | None:
        """Return a database's stored DatabaseInput (+ CreateTime), if any."""
        rows = self._q("SELECT input, created FROM databases WHERE name = ?", (name,))
        if not rows:
            return None
        return {**json.loads(rows[0][0]), "CreateTime": rows[0][1]}

    def databases(self) -> list[str]:
        """Return the names of databases this store knows."""
        return [r[0] for r in self._q("SELECT name FROM databases ORDER BY name")]

    def put_database(self, name: str, db_input: dict) -> None:
        """Create or replace a database's metadata."""
        existing = self.database(name)
        created = existing["CreateTime"] if existing else time.time()
        self._q(
            "INSERT OR REPLACE INTO databases VALUES (?, ?, ?)",
            (name, json.dumps(db_input), created),
        )

    def delete_database(self, name: str) -> None:
        """Forget a database and every table and partition in it."""
        self._q("DELETE FROM column_stats WHERE db = ?", (name,))
        self._q("DELETE FROM partitions WHERE db = ?", (name,))
        self._q("DELETE FROM tables WHERE db = ?", (name,))
        self._q("DELETE FROM databases WHERE name = ?", (name,))

    # -------------------------------------------------------------------------
    # Connections
    # -------------------------------------------------------------------------
    def connection(self, name: str) -> dict | None:
        """Return a stored connection as Glue returns it, or None."""
        rows = self._q(
            "SELECT input, created, updated FROM connections WHERE name = ?", (name,)
        )
        if not rows:
            return None
        conn_input, created, updated = rows[0]
        return {
            **json.loads(conn_input),
            "CreationTime": created,
            "LastUpdatedTime": updated,
        }

    def connections(self) -> list[str]:
        """Return the names of stored connections."""
        return [r[0] for r in self._q("SELECT name FROM connections ORDER BY name")]

    def put_connection(self, name: str, conn_input: dict) -> None:
        """Create or replace a connection."""
        existing = self.connection(name)
        now = time.time()
        created = existing["CreationTime"] if existing else now
        self._q(
            "INSERT OR REPLACE INTO connections VALUES (?, ?, ?, ?)",
            (name, json.dumps(conn_input), created, now),
        )

    def delete_connection(self, name: str) -> None:
        """Forget a connection."""
        self._q("DELETE FROM connections WHERE name = ?", (name,))

    # -------------------------------------------------------------------------
    # Tables
    # -------------------------------------------------------------------------
    def table(self, db: str, name: str) -> dict | None:
        """Return a stored table as Glue returns it, or None."""
        rows = self._q(
            "SELECT input, created, updated, version FROM tables "
            "WHERE db = ? AND name = ?",
            (db, name.lower()),
        )
        if not rows:
            return None
        table_input, created, updated, version = rows[0]
        return {
            **json.loads(table_input),
            "DatabaseName": db,
            "CreateTime": created,
            "UpdateTime": updated,
            "VersionId": str(version),
        }

    def tables(self, db: str) -> list[dict]:
        """Return every stored table in a database."""
        names = [
            r[0]
            for r in self._q(
                "SELECT name FROM tables WHERE db = ? ORDER BY name", (db,)
            )
        ]
        return [t for t in (self.table(db, n) for n in names) if t]

    def put_table(self, db: str, table_input: dict) -> None:
        """Create or replace a table definition, bumping its version."""
        name = table_input["Name"].lower()
        existing = self._q(
            "SELECT created, version FROM tables WHERE db = ? AND name = ?", (db, name)
        )
        now = time.time()
        created, version = existing[0] if existing else (now, 0)
        self._q(
            "INSERT OR REPLACE INTO tables VALUES (?, ?, ?, ?, ?, ?)",
            (
                db,
                name,
                json.dumps({**table_input, "Name": name}),
                created,
                now,
                version + 1,
            ),
        )

    def delete_table(self, db: str, name: str) -> bool:
        """Delete a table and its partitions; return whether it existed."""
        name = name.lower()
        existed = bool(
            self._q("SELECT 1 FROM tables WHERE db = ? AND name = ?", (db, name))
        )
        self._q("DELETE FROM column_stats WHERE db = ? AND tbl = ?", (db, name))
        self._q("DELETE FROM partitions WHERE db = ? AND tbl = ?", (db, name))
        self._q("DELETE FROM tables WHERE db = ? AND name = ?", (db, name))
        return existed

    # -------------------------------------------------------------------------
    # Partitions
    # -------------------------------------------------------------------------
    @staticmethod
    def _key(values: list[str]) -> str:
        """Return the partition values as the JSON key they are stored under."""
        return json.dumps(list(values))

    def partition(self, db: str, tbl: str, values: list[str]) -> dict | None:
        """Return one partition as Glue returns it, or None."""
        rows = self._q(
            "SELECT input, created FROM partitions WHERE db = ? AND tbl = ? AND vals = ?",
            (db, tbl.lower(), self._key(values)),
        )
        if not rows:
            return None
        return {
            **json.loads(rows[0][0]),
            "Values": list(values),
            "DatabaseName": db,
            "TableName": tbl.lower(),
            "CreationTime": rows[0][1],
        }

    def partitions(self, db: str, tbl: str) -> list[dict]:
        """Return every partition of a table."""
        rows = self._q(
            "SELECT vals FROM partitions WHERE db = ? AND tbl = ? ORDER BY vals",
            (db, tbl.lower()),
        )
        found = (self.partition(db, tbl, json.loads(r[0])) for r in rows)
        return [p for p in found if p]

    def put_partition(self, db: str, tbl: str, part_input: dict) -> bool:
        """Create or replace a partition; return False if it already existed."""
        key = self._key(part_input["Values"])
        existed = bool(
            self._q(
                "SELECT 1 FROM partitions WHERE db = ? AND tbl = ? AND vals = ?",
                (db, tbl.lower(), key),
            )
        )
        self._q(
            "INSERT OR REPLACE INTO partitions VALUES (?, ?, ?, ?, ?)",
            (db, tbl.lower(), key, json.dumps(part_input), time.time()),
        )
        return not existed

    def delete_partition(self, db: str, tbl: str, values: list[str]) -> bool:
        """Delete a partition; return whether it existed."""
        key = self._key(values)
        existed = bool(
            self._q(
                "SELECT 1 FROM partitions WHERE db = ? AND tbl = ? AND vals = ?",
                (db, tbl.lower(), key),
            )
        )
        self._q(
            "DELETE FROM partitions WHERE db = ? AND tbl = ? AND vals = ?",
            (db, tbl.lower(), key),
        )
        self._q(
            "DELETE FROM column_stats WHERE db = ? AND tbl = ? AND vals = ?",
            (db, tbl.lower(), key),
        )
        return existed

    # -------------------------------------------------------------------------
    # Column statistics (values=None: the table's own)
    # -------------------------------------------------------------------------
    def column_stats(
        self, db: str, tbl: str, values: list[str] | None, columns: list[str]
    ) -> list[dict]:
        """Return the stored ColumnStatistics for the named columns."""
        vals = self._key(values) if values is not None else ""
        found = []
        for col in columns:
            rows = self._q(
                "SELECT stats FROM column_stats "
                "WHERE db = ? AND tbl = ? AND vals = ? AND col = ?",
                (db, tbl.lower(), vals, col.lower()),
            )
            if rows:
                found.append(json.loads(rows[0][0]))
        return found

    def put_column_stats(
        self, db: str, tbl: str, values: list[str] | None, stats: dict
    ) -> None:
        """Store one column's ColumnStatistics, replacing what was there."""
        vals = self._key(values) if values is not None else ""
        self._q(
            "INSERT OR REPLACE INTO column_stats VALUES (?, ?, ?, ?, ?)",
            (db, tbl.lower(), vals, stats["ColumnName"].lower(), json.dumps(stats)),
        )

    def delete_column_stats(
        self, db: str, tbl: str, values: list[str] | None, column: str
    ) -> None:
        """Forget one column's statistics."""
        vals = self._key(values) if values is not None else ""
        self._q(
            "DELETE FROM column_stats WHERE db = ? AND tbl = ? AND vals = ? AND col = ?",
            (db, tbl.lower(), vals, column.lower()),
        )
