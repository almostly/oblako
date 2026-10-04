"""Redshift's Apache Iceberg tables: external schemas, CREATE TABLE ... USING ICEBERG.

Redshift writes Iceberg tables registered in the AWS Glue Data Catalog. oblako's
Glue catalog keeps Iceberg tables in its Iceberg REST catalog, on S3Proxy, so a
table Redshift creates here is the same table Athena, Trino, Spark and PyIceberg
see, and one they create shows up in Redshift.

The wire proxy rewrites the Redshift-only DDL into calls to the plpython3u
functions in ``initdb.d/13_iceberg.sql``, which land here:

- ``CREATE EXTERNAL SCHEMA s FROM DATA CATALOG DATABASE 'db'`` makes schema ``s``
  for Glue database (Iceberg namespace) ``db``, with a view for each table in it.
- ``CREATE TABLE s.t (...) USING ICEBERG [LOCATION] [PARTITIONED BY]
  [TABLE PROPERTIES] [AS query]`` creates the Iceberg table and its view.
- ``SHOW TABLE s.t`` returns the table's DDL.

Everything else is plain PostgreSQL on the view. ``SELECT`` scans the Iceberg
table, so joins with local tables just work. ``INSERT``, ``UPDATE`` and
``DELETE`` go through INSTEAD OF triggers into a staging table, and a deferred
trigger commits the staged rows to Iceberg at COMMIT: an insert-only write is an
append, anything else rewrites the table (copy-on-write). As on Redshift, a
statement is its own transaction, ROLLBACK writes nothing, and a transaction takes
one Iceberg write. ``DROP TABLE s.t`` removes the table from the catalog and keeps
its files, as Redshift does.

Self-contained (no ``oblako`` imports), like ``copy_unload``: it is copied into the
redshift image next to ``redshift_proxy.py``. Only the standard library is
imported at module level; pyiceberg and pyarrow are imported by the engine-side
functions.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from typing import NoReturn

# -----------------------------------------------------------------------------------------------
# Proxy side: rewrite the Redshift-only DDL into function calls
# -----------------------------------------------------------------------------------------------
_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
_NAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}}"

_EXTERNAL_SCHEMA = re.compile(
    rf"^\s*create\s+external\s+schema\s+(?P<ine>if\s+not\s+exists\s+)?"
    rf"(?P<schema>{_IDENT})\s+from\s+data\s+catalog\b(?P<rest>.*?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_DATABASE = re.compile(r"(?i)\bdatabase\s+'((?:[^']|'')*)'")
_CREATE_DB = re.compile(r"(?i)\bcreate\s+external\s+database\s+if\s+not\s+exists\b")
_CREATE_ICEBERG = re.compile(
    rf"^\s*create\s+table\s+(?P<ine>if\s+not\s+exists\s+)?(?P<name>{_NAME})\s*",
    re.IGNORECASE | re.DOTALL,
)
_USING_ICEBERG = re.compile(r"(?i)^\s*using\s+iceberg\b")
_LOCATION = re.compile(r"(?i)^\s*location\s+'((?:[^']|'')*)'")
_PARTITIONED = re.compile(r"(?i)^\s*partitioned\s+by\b\s*")
_PROPERTIES = re.compile(r"(?i)^\s*table\s+properties\s*")
_AS = re.compile(r"(?i)^\s*as\b\s*(?P<query>.*?)\s*;?\s*$", re.DOTALL)
_PARTITION_END = re.compile(r"(?i)\btable\s+properties\b|\bas\b|;")
_SHOW_TABLE = re.compile(
    rf"^\s*show\s+table\s+(?P<name>{_NAME})\s*;?\s*$", re.IGNORECASE | re.DOTALL
)


def _literal(value: str | None) -> str:
    """Return ``value`` as a SQL literal, dollar-quoted, so any text passes through intact."""
    if value is None:
        return "NULL"
    tag = "$ice$"
    n = 0
    while tag in value:
        n += 1
        tag = f"$ice{n}$"
    return f"{tag}{value}{tag}"


def _balanced(text: str, start: int) -> int:
    """Index just past the parenthesis group opening at ``text[start]``.

    Quoted strings and identifiers are skipped, so parentheses inside them don't
    count. Returns -1 when the group never closes.
    """
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "'\"":
            end = text.find(ch, i + 1)
            while end != -1 and end + 1 < len(text) and text[end + 1] == ch:
                end = text.find(ch, end + 2)
            if end == -1:
                return -1
            i = end + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def parse_create_iceberg(sql: str) -> dict | None:
    """Parse ``CREATE TABLE ... USING ICEBERG``; None for any other statement."""
    m = _CREATE_ICEBERG.match(sql)
    if not m:
        return None
    rest = sql[m.end() :]
    columns = None
    if rest.startswith("("):
        end = _balanced(rest, 0)
        if end == -1:
            return None
        columns, rest = rest[1 : end - 1].strip(), rest[end:]
    u = _USING_ICEBERG.match(rest)
    if not u:
        return None
    rest = rest[u.end() :]
    out = {
        "name": m.group("name"),
        "if_not_exists": bool(m.group("ine")),
        "columns": columns,
        "location": None,
        "partitioned": None,
        "properties": None,
        "query": None,
    }
    while rest.strip() and rest.strip() != ";":
        if lm := _LOCATION.match(rest):
            out["location"] = lm.group(1).replace("''", "'")
            rest = rest[lm.end() :]
        elif pm := _PARTITIONED.match(rest):
            rest = rest[pm.end() :]
            if rest.startswith("("):
                end = _balanced(rest, 0)
                if end == -1:
                    return None
                out["partitioned"], rest = rest[1 : end - 1].strip(), rest[end:]
            else:
                stop = _PARTITION_END.search(rest)
                cut = stop.start() if stop else len(rest)
                out["partitioned"], rest = rest[:cut].strip(), rest[cut:]
        elif tm := _PROPERTIES.match(rest):
            rest = rest[tm.end() :]
            end = _balanced(rest, 0) if rest.startswith("(") else -1
            if end == -1:
                return None
            out["properties"], rest = rest[1 : end - 1].strip(), rest[end:]
        elif am := _AS.match(rest):
            out["query"] = am.group("query")
            rest = ""
        else:
            return None
    return out


def _command(call: str) -> str:
    """Wrap a pg_oblako call so the client sees a command, with no result rows.

    Redshift answers DDL with a command completion and no result set; a ``DO``
    block does the same (``SELECT`` would hand back a status row).
    """
    tag = "$oblako_ddl$"
    n = 0
    while tag in call:
        n += 1
        tag = f"$oblako_ddl{n}$"
    return f"DO {tag} BEGIN PERFORM {call}; END {tag}"


def rewrite_iceberg(sql: str) -> str:
    """Rewrite external-schema and Iceberg DDL into pg_oblako function calls."""
    if m := _EXTERNAL_SCHEMA.match(sql):
        db = _DATABASE.search(m.group("rest"))
        database = db.group(1).replace("''", "'") if db else None
        return _command(
            "pg_oblako.create_external_schema("
            f"{_literal(m.group('schema'))}, {_literal(database)}, "
            f"{'true' if _CREATE_DB.search(m.group('rest')) else 'false'}, "
            f"{'true' if m.group('ine') else 'false'})"
        )
    if (p := parse_create_iceberg(sql)) is not None:
        return _command(
            "pg_oblako.iceberg_create_table("
            f"{_literal(p['name'])}, {_literal(p['columns'])}, "
            f"{_literal(p['location'])}, {_literal(p['partitioned'])}, "
            f"{_literal(p['properties'])}, "
            f"{'true' if p['if_not_exists'] else 'false'}, {_literal(p['query'])})"
        )
    if m := _SHOW_TABLE.match(sql):
        return (
            f"SELECT pg_oblako.show_table({_literal(m.group('name'))}) "
            'AS "Show Table DDL statement"'
        )
    return sql


_MERGE = re.compile(
    rf"^\s*merge\s+into\s+(?P<name>{_NAME})(?P<rest>\s.*?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def merge_target(sql: str) -> list[str] | None:
    """Return the target's name parts if ``sql`` is a MERGE, else None."""
    m = _MERGE.match(sql)
    return split_name(m.group("name")) if m else None


_ALTER = re.compile(
    rf"^\s*alter\s+table\s+(?:if\s+exists\s+)?(?P<name>{_NAME})\s+(?P<action>.+?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def alter_target(sql: str) -> list[str] | None:
    """Return the target's name parts if ``sql`` is an ALTER TABLE, else None."""
    m = _ALTER.match(sql)
    return split_name(m.group("name")) if m else None


def rewrite_alter(sql: str) -> str:
    """Rewrite an ALTER TABLE on an Iceberg table into a pg_oblako call.

    The proxy calls this only for a target in an external schema: Redshift's
    Iceberg ALTERs (SET TABLE PROPERTIES, ... PARTITION FIELD) aren't PostgreSQL
    syntax, and PostgreSQL can't add or drop a view's columns.
    """
    m = _ALTER.match(sql)
    if m is None:
        return sql
    return _command(
        f"pg_oblako.iceberg_alter_table({_literal(m.group('name'))}, "
        f"{_literal(m.group('action'))})"
    )


def rewrite_merge(sql: str) -> str:
    """Rewrite a MERGE into an Iceberg table into a pg_oblako call.

    PostgreSQL 16 can't MERGE into a view, which is what an Iceberg table is
    here; ``iceberg_merge`` runs the statement against a copy instead. The proxy
    calls this only for a target in an external schema; any other MERGE runs as
    sent.
    """
    return _command(f"pg_oblako.iceberg_merge({_literal(sql)})")


def has_iceberg_ddl(sql: str) -> bool:
    """Cheap test for statements ``rewrite_iceberg`` may change."""
    s = sql.lstrip().lower()
    return s.startswith(("create", "show")) and (
        "external" in s or "iceberg" in s or s.startswith("show")
    )


# -----------------------------------------------------------------------------------------------
# Names, properties and partition specs
# -----------------------------------------------------------------------------------------------
def split_name(name: str) -> list[str]:
    """Split a dotted SQL name into parts: quoted parts keep case, others fold to lower."""
    parts = re.findall(rf"\s*({_IDENT})\s*(?:\.|$)", name)
    return [
        p[1:-1].replace('""', '"') if p.startswith('"') else p.lower() for p in parts
    ]


def _quote(ident: str) -> str:
    """Quote an identifier for SQL."""
    return '"' + ident.replace('"', '""') + '"'


_PROPERTY = re.compile(r"'((?:[^']|'')*)'\s*=\s*'((?:[^']|'')*)'")
_COMPRESSIONS = {"zstd", "brotli", "gzip", "snappy", "uncompressed"}


def parse_properties(text: str | None) -> dict[str, str]:
    """Parse ``'key'='value', ...`` from TABLE PROPERTIES, checked as Redshift does."""
    props: dict[str, str] = {}
    for key, value in _PROPERTY.findall(text or ""):
        key, value = key.replace("''", "'").lower(), value.replace("''", "'")
        # Redshift's own messages. v3 is refused as Redshift Serverless refused it
        # (2026-10); oblako's catalog and pyiceberg write v2 only besides.
        if key == "format-version":
            if value != "2":
                raise ValueError(
                    f'"{value}" is not a valid value for the "format-version" '
                    'property of "iceberg" table'
                )
        elif key == "compression_type":
            value = value.lower()
            if value not in _COMPRESSIONS:
                raise ValueError(
                    f'"{value}" is not a valid value for the "compression_type" '
                    'property of "iceberg" table'
                )
        else:
            raise ValueError(
                f'"{key}" cannot be used in the PROPERTIES clause of "iceberg" table'
            )
        props[key] = value
    return props


_TRANSFORM = re.compile(
    rf"^(?P<fn>bucket|truncate|year|month|day|hour)\s*\(\s*"
    rf"(?:(?P<n>\d+)\s*,\s*)?(?P<col>{_IDENT})\s*\)$",
    re.IGNORECASE,
)


def parse_partitions(text: str | None) -> list[tuple[str, str, int | None]]:
    """Parse PARTITIONED BY into ``(column, transform, argument)`` triples."""
    if not text:
        return []
    out: list[tuple[str, str, int | None]] = []
    depth, start = 0, 0
    items = []
    for i, ch in enumerate(text):
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            items.append(text[start:i])
            start = i + 1
    items.append(text[start:])
    for item in (x.strip() for x in items if x.strip()):
        if m := _TRANSFORM.match(item):
            fn, n, col = (
                m.group("fn").lower(),
                m.group("n"),
                split_name(m.group("col"))[0],
            )
            if fn in ("bucket", "truncate") and n is None:
                raise ValueError(f"{fn} takes a width: {fn}(N, column)")
            if fn not in ("bucket", "truncate") and n is not None:
                raise ValueError(f"{fn} takes only a column: {fn}(column)")
            out.append((col, fn, int(n) if n else None))
        elif re.fullmatch(_IDENT, item):
            out.append((split_name(item)[0], "identity", None))
        else:
            raise ValueError(f"unsupported partition transform: {item}")
    cols = [c for c, _, _ in out]
    for c in cols:
        if cols.count(c) > 1:
            raise ValueError(
                f'"{c}"  used in multiple transform functions for "iceberg" table'
            )
    return out


# -----------------------------------------------------------------------------------------------
# Engine side: the Iceberg REST catalog
# -----------------------------------------------------------------------------------------------
_CATALOG = None


def iceberg_url() -> str:
    """Where oblako's Iceberg REST catalog answers (the Glue catalog's Iceberg side)."""
    return os.environ.get("OBLAKO_ICEBERG_URL") or "http://host.docker.internal:8181"


def catalog():
    """Load the Iceberg REST catalog, with S3 on oblako's S3Proxy (cached per session)."""
    global _CATALOG
    if _CATALOG is None:
        # S3Proxy has no flexible checksums
        os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
        os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")
        from pyiceberg.catalog import load_catalog

        props = {
            "type": "rest",
            "uri": iceberg_url(),
            "py-io-impl": "pyiceberg.io.fsspec.FsspecFileIO",
            "s3.path-style-access": "true",
            "s3.region": os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1",
        }
        endpoint = os.environ.get("OBLAKO_S3_ENDPOINT") or os.environ.get(
            "AWS_ENDPOINT_URL_S3"
        )
        if endpoint:
            props["s3.endpoint"] = endpoint
            # S3Proxy runs with auth disabled; any credentials do
            props["s3.access-key-id"] = os.environ.get("AWS_ACCESS_KEY_ID", "oblako")
            props["s3.secret-access-key"] = os.environ.get(
                "AWS_SECRET_ACCESS_KEY", "oblako"
            )
        _CATALOG = load_catalog("oblako", **props)
    return _CATALOG


def _fail(plpy, message: str) -> NoReturn:
    """Raise ``message`` as the statement's error (``plpy.error`` raises)."""
    plpy.error(message)
    raise RuntimeError(message)


@contextmanager
def _coordinator_only(plpy):
    """Keep oblako's own DDL on this node: Citus must not replay it on workers.

    The views, staging tables and their triggers serve this node's clients, and a
    worker refuses the pg_oblako schema. Without Citus the setting is inert.
    """
    old = plpy.execute(
        "SELECT current_setting('citus.enable_ddl_propagation', true) AS v"
    )[0]["v"]
    plpy.execute("SELECT set_config('citus.enable_ddl_propagation', 'off', true)")
    try:
        yield
    finally:
        plpy.execute(
            "SELECT set_config('citus.enable_ddl_propagation', "
            f"{plpy.quote_literal(old or 'on')}, true)"
        )


def _catalog_or_error(plpy):
    """Return the catalog, or raise a clear error when oblako's Iceberg catalog isn't running."""
    try:
        cat = catalog()
        cat.list_namespaces()
        return cat
    except Exception as e:  # unreachable, refused, or a bad answer
        global _CATALOG
        _CATALOG = None
        plpy.error(
            f"oblako's Iceberg catalog at {iceberg_url()} is not reachable ({e}). "
            "Start it with `oblako up iceberg`."
        )


# -----------------------------------------------------------------------------------------------
# Types
# -----------------------------------------------------------------------------------------------
_INT_TYPES = {"smallint", "integer"}


def iceberg_type(pg_type: str):
    """Return the Iceberg type for a PostgreSQL column type (``format_type`` output)."""
    from pyiceberg import types as t

    pg = pg_type.lower()
    if pg in _INT_TYPES:
        return t.IntegerType()
    if pg == "bigint":
        return t.LongType()
    if pg == "real":
        return t.FloatType()
    if pg == "double precision":
        return t.DoubleType()
    if m := re.fullmatch(r"numeric\((\d+),(\d+)\)", pg):
        return t.DecimalType(int(m.group(1)), int(m.group(2)))
    if pg == "boolean":
        return t.BooleanType()
    if pg == "text" or pg.startswith(("character varying", "character(", "character")):
        return t.StringType()
    if pg == "date":
        return t.DateType()
    if pg == "timestamp without time zone":
        return t.TimestampType()
    if pg == "timestamp with time zone":
        return t.TimestamptzType()
    if pg == "time without time zone":
        return t.TimeType()
    if pg == "bytea":
        return t.BinaryType()
    raise ValueError(f"type {pg_type} is not supported in Iceberg tables")


def pg_type(ice) -> str | None:
    """Return the PostgreSQL column type for an Iceberg type; None when unsupported."""
    from pyiceberg import types as t

    if isinstance(ice, t.IntegerType):
        return "integer"
    if isinstance(ice, t.LongType):
        return "bigint"
    if isinstance(ice, t.FloatType):
        return "real"
    if isinstance(ice, t.DoubleType):
        return "double precision"
    if isinstance(ice, t.DecimalType):
        return f"numeric({ice.precision},{ice.scale})"
    if isinstance(ice, t.BooleanType):
        return "boolean"
    if isinstance(ice, (t.StringType, t.UUIDType)):
        return "character varying"  # Redshift shows an Iceberg string as varchar
    if isinstance(ice, t.DateType):
        return "date"
    if isinstance(ice, t.TimestamptzType):
        return "timestamp with time zone"
    if isinstance(ice, t.TimestampType):
        return "timestamp without time zone"
    if isinstance(ice, t.TimeType):
        return "time without time zone"
    if isinstance(ice, (t.BinaryType, t.FixedType)):
        return "bytea"
    if isinstance(ice, (t.StructType, t.ListType, t.MapType)):
        return "super"
    return None


# -----------------------------------------------------------------------------------------------
# External schemas
# -----------------------------------------------------------------------------------------------
def _registered_schema(plpy, schema: str) -> str | None:
    """Return the Glue database behind external schema ``schema``, or None."""
    rows = plpy.execute(
        "SELECT databasename FROM pg_oblako.external_schemas WHERE schemaname = "
        + plpy.quote_literal(schema)
    )
    return rows[0]["databasename"] if rows.nrows() else None


def _create_external_schema(
    plpy, schema: str, database: str | None, create_db: bool, if_not_exists: bool
) -> str:
    """CREATE EXTERNAL SCHEMA ... FROM DATA CATALOG: a schema for a Glue database."""
    from pyiceberg.exceptions import NoSuchNamespaceError

    schema = split_name(schema)[0]
    if not database:
        _fail(
            plpy, "CREATE EXTERNAL SCHEMA ... FROM DATA CATALOG needs DATABASE 'name'"
        )
    cat = _catalog_or_error(plpy)
    try:
        cat.load_namespace_properties(database)
    except NoSuchNamespaceError:
        if not create_db:
            plpy.error(
                f'database "{database}" does not exist in the Data Catalog; add '
                "CREATE EXTERNAL DATABASE IF NOT EXISTS to create it"
            )
        cat.create_namespace(database)
    exists = plpy.execute(
        "SELECT 1 FROM pg_namespace WHERE nspname = " + plpy.quote_literal(schema)
    ).nrows()
    if exists and if_not_exists:
        # run again, it picks up tables other engines created since
        if _registered_schema(plpy, schema) == database:
            _attach_new_tables(plpy, cat, schema, database)
        return "CREATE EXTERNAL SCHEMA"
    plpy.execute(f"CREATE SCHEMA {_quote(schema)}")
    plpy.execute(
        "INSERT INTO pg_oblako.external_schemas (schemaname, databasename) VALUES ("
        f"{plpy.quote_literal(schema)}, {plpy.quote_literal(database)})"
    )
    _attach_new_tables(plpy, cat, schema, database)
    return "CREATE EXTERNAL SCHEMA"


def _attach_new_tables(plpy, cat, schema: str, database: str) -> None:
    """Make a view for each table of ``database`` that ``schema`` doesn't have yet."""
    known = {
        r["tablename"]
        for r in plpy.execute(
            "SELECT tablename FROM pg_oblako.iceberg_tables WHERE schemaname = "
            + plpy.quote_literal(schema)
        )
    }
    for _, name in cat.list_tables(database):
        if name in known:
            continue
        try:
            _attach(plpy, schema, database, name, cat.load_table((database, name)))
        except ValueError as e:
            plpy.notice(f"skipped {database}.{name}: {e}")


# -----------------------------------------------------------------------------------------------
# Tables: the view, its staging table and triggers
# -----------------------------------------------------------------------------------------------
def _columns(plpy, relation: str) -> list[tuple[str, str]]:
    """``(name, format_type)`` of a relation's columns, oblako's own left out."""
    rows = plpy.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS t "
        "FROM pg_attribute a WHERE a.attrelid = "
        f"{plpy.quote_literal(relation)}::regclass AND a.attnum > 0 "
        "AND NOT a.attisdropped AND a.attname NOT IN ('_op', '_stmt') "
        "ORDER BY a.attnum"
    )
    return [(r["attname"], r["t"]) for r in rows]


def _not_null_columns(plpy, relation: str) -> set[str]:
    """Return the NOT NULL columns of ``relation``."""
    rows = plpy.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = "
        f"{plpy.quote_literal(relation)}::regclass AND attnum > 0 "
        "AND NOT attisdropped AND attnotnull"
    )
    return {r["attname"] for r in rows}


def _attach(plpy, schema, database, name, table, stage=None, location=None) -> None:
    """Create the view, triggers and registry row for Iceberg table ``database.name``.

    ``stage`` is an existing staging table (CREATE TABLE made it from the column
    list); otherwise one is made from the Iceberg schema.
    """
    if stage is None:
        cols = []
        for field in table.schema().fields:
            pgt = pg_type(field.field_type)
            if pgt is None:
                raise ValueError(f"column {field.name} has type {field.field_type}")
            cols.append(
                f"{_quote(field.name)} {pgt}" + (" NOT NULL" if field.required else "")
            )
        n = plpy.execute("SELECT nextval('pg_oblako.iceberg_stage_seq') AS n")[0]["n"]
        stage = f"pg_oblako.iceberg_stage_{n}"
        plpy.execute(
            f'CREATE UNLOGGED TABLE {stage} (_op "char", _stmt timestamptz, '
            + ", ".join(cols)
            + ")"
        )
    columns = _columns(plpy, stage)
    names = ", ".join(_quote(c) for c, _ in columns)
    defs = ", ".join(f"{_quote(c)} {t}" for c, t in columns)
    view = f"{_quote(schema)}.{_quote(name)}"
    plpy.execute(
        f"CREATE VIEW {view} AS "
        f"(SELECT {names} FROM pg_oblako.iceberg_scan("
        f"{plpy.quote_literal(database)}, {plpy.quote_literal(name)}) AS r({defs}) "
        f"EXCEPT ALL SELECT {names} FROM {stage} WHERE _op = 'D') "
        f"UNION ALL SELECT {names} FROM {stage} WHERE _op = 'I'"
    )
    fn = f"{stage}_dml"
    required = _not_null_columns(plpy, stage)
    null_checks = "".join(
        f"    IF NEW.{_quote(c)} IS NULL THEN\n"
        "      RAISE EXCEPTION 'Cannot insert a NULL value into column "
        + c.replace("'", "''")
        + "';\n"
        "    END IF;\n"
        for c, _ in columns
        if c in required
    )
    if null_checks:
        null_checks = "  IF TG_OP <> 'DELETE' THEN\n" + null_checks + "  END IF;\n"
    plpy.execute(
        f"CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $fn$\n"
        "BEGIN\n"
        f"  IF EXISTS (SELECT 1 FROM {stage} WHERE _stmt < statement_timestamp()\n"
        "             OR _stmt > statement_timestamp()) THEN\n"
        "    RAISE EXCEPTION 'a transaction takes one write to Iceberg table %, as on "
        f"Redshift: COMMIT first', {plpy.quote_literal(schema + '.' + name)};\n"
        "  END IF;\n" + null_checks + "  IF TG_OP <> 'INSERT' THEN\n"
        f"    INSERT INTO {stage} SELECT 'D', statement_timestamp(), OLD.*;\n"
        "  END IF;\n"
        "  IF TG_OP <> 'DELETE' THEN\n"
        f"    INSERT INTO {stage} SELECT 'I', statement_timestamp(), NEW.*;\n"
        "    RETURN NEW;\n"
        "  END IF;\n"
        "  RETURN OLD;\n"
        "END $fn$"
    )
    plpy.execute(
        f"CREATE TRIGGER iceberg_dml INSTEAD OF INSERT OR UPDATE OR DELETE ON {view} "
        f"FOR EACH ROW EXECUTE FUNCTION {fn}()"
    )
    plpy.execute(f"CREATE INDEX ON {stage} (_stmt)")
    plpy.execute(
        f"CREATE CONSTRAINT TRIGGER iceberg_commit AFTER INSERT ON {stage} "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION pg_oblako.iceberg_commit()"
    )
    plpy.execute(
        "INSERT INTO pg_oblako.iceberg_tables "
        "(schemaname, tablename, databasename, location, stage) VALUES ("
        f"{plpy.quote_literal(schema)}, {plpy.quote_literal(name)}, "
        f"{plpy.quote_literal(database)}, "
        f"{plpy.quote_literal(location or table.location())}, "
        f"{plpy.quote_literal(stage)})"
    )


def _location_is_empty(location: str) -> bool:
    """Whether no S3 object lives under ``location`` (Redshift requires it empty)."""
    import boto3
    from botocore.config import Config

    bucket, _, prefix = location.removeprefix("s3://").partition("/")
    endpoint = os.environ.get("OBLAKO_S3_ENDPOINT") or os.environ.get(
        "AWS_ENDPOINT_URL_S3"
    )
    kwargs: dict = {"config": Config(s3={"addressing_style": "path"})}
    if endpoint:
        kwargs.update(
            endpoint_url=endpoint,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "oblako"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "oblako"),
            region_name="us-east-1",
        )
    s3 = boto3.client("s3", **kwargs)
    try:
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    except s3.exceptions.NoSuchBucket:
        raise ValueError(f"bucket {bucket} does not exist") from None
    return not listed.get("KeyCount")


def _transform(fn: str, arg: int | None):
    """Return the pyiceberg transform for a parsed PARTITIONED BY item."""
    from pyiceberg import transforms as t

    if fn == "bucket" and arg is not None:
        return t.BucketTransform(arg)
    if fn == "truncate" and arg is not None:
        return t.TruncateTransform(arg)
    simple = {
        "identity": t.IdentityTransform,
        "year": t.YearTransform,
        "month": t.MonthTransform,
        "day": t.DayTransform,
        "hour": t.HourTransform,
    }
    return simple[fn]()


def _create_table(
    plpy,
    name: str,
    columns: str | None,
    location: str | None,
    partitioned: str | None,
    properties: str | None,
    if_not_exists: bool,
    query: str | None,
) -> str:
    """CREATE TABLE ... USING ICEBERG, and its CREATE TABLE AS SELECT form."""
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.types import NestedField

    parts = split_name(name)
    if len(parts) != 2:
        plpy.error(
            "name an Iceberg table <external_schema>.<table>; oblako does not "
            "support three-part catalog names yet"
        )
    schema, table = parts
    database = _registered_schema(plpy, schema)
    if database is None:
        plpy.error(
            f'schema "{schema}" is not an external schema: create it with '
            "CREATE EXTERNAL SCHEMA ... FROM DATA CATALOG"
        )
    try:
        props = parse_properties(properties)
        partitions = parse_partitions(partitioned)
    except ValueError as e:
        plpy.error(str(e))
    cat = _catalog_or_error(plpy)
    if cat.table_exists((database, table)):
        if if_not_exists:
            plpy.notice(f'table "{table}" already exists, skipping')
            return "CREATE TABLE"
        plpy.error(f'table "{table}" already exists')
    # Redshift's own messages, from a Redshift Serverless run (2026-10)
    if not location:
        _fail(plpy, f'Empty location for Iceberg table "{table}"')
    location = location.rstrip("/")
    try:
        if not _location_is_empty(location):
            plpy.error(
                f'Cannot create Iceberg table: S3 location "{location}" contains '
                "existing objects"
            )
    except ValueError as e:
        plpy.error(str(e))
    version = props.get("format-version", "2")
    constraints = (
        'Columns constraints and attributes are not supported for an "iceberg" table.'
    )
    if columns and re.search(r"(?i)\bdefault\b", columns):
        plpy.error(
            constraints,
            hint='Default values are only supported with Iceberg version "3".',
        )
    # NOT NULL is the one column attribute Redshift takes
    if columns and re.search(
        r"(?i)\b(primary\s+key|unique|references|identity|encode|distkey|sortkey|"
        r"collate|check)\b",
        columns,
    ):
        plpy.error(constraints)

    n = plpy.execute("SELECT nextval('pg_oblako.iceberg_stage_seq') AS n")[0]["n"]
    stage = f"pg_oblako.iceberg_stage_{n}"
    if query is None:
        if not columns:
            plpy.error("CREATE TABLE ... USING ICEBERG needs a column list")
        plpy.execute(
            f'CREATE UNLOGGED TABLE {stage} (_op "char", _stmt timestamptz, {columns})'
        )
    else:
        renamed = ""
        if columns:
            renamed = "(_op, _stmt, " + columns + ")"
        plpy.execute(
            f"CREATE UNLOGGED TABLE {stage} {renamed} AS "
            f"SELECT 'I'::\"char\" AS _op, statement_timestamp() AS _stmt, q.* "
            f"FROM ({query}) q WITH NO DATA"
        )
    # Redshift's DECIMAL without precision is DECIMAL(18,0)
    for col, typ in _columns(plpy, stage):
        if typ == "numeric":
            plpy.execute(
                f"ALTER TABLE {stage} ALTER COLUMN {_quote(col)} TYPE numeric(18,0)"
            )
    not_null = _not_null_columns(plpy, stage)
    fields = []
    try:
        for i, (col, typ) in enumerate(_columns(plpy, stage), start=1):
            if query is None and typ.startswith("character varying("):
                plpy.error(
                    f'VARCHAR(N) specifiying length is not supported for column "{col}" '
                    "in Iceberg table.",
                    hint="Use VARCHAR for strings in Iceberg tables.",
                )
            fields.append(
                NestedField(i, col, iceberg_type(typ), required=col in not_null)
            )
    except ValueError as e:
        plpy.error(str(e))
    ice_schema = Schema(*fields)
    by_name = {f.name: f.field_id for f in fields}
    spec_fields = []
    for i, (col, fn, arg) in enumerate(partitions, start=1000):
        if col not in by_name:
            plpy.error(f'partition column "{col}" is not a column of the table')
        label = col if fn == "identity" else f"{col}_{fn}"
        spec_fields.append(PartitionField(by_name[col], i, _transform(fn, arg), label))
    table_props = {"format-version": version}
    table_props["write.parquet.compression-codec"] = props.get(
        "compression_type", "zstd"
    )
    try:
        created = cat.create_table(
            (database, table),
            schema=ice_schema,
            location=location.rstrip("/"),
            partition_spec=PartitionSpec(*spec_fields),
            properties=table_props,
        )
    except Exception as e:  # the catalog's refusal, as the error
        plpy.error(f"creating Iceberg table {database}.{table} failed: {e}")
    _attach(plpy, schema, database, table, created, stage=stage, location=location)
    if query is not None:
        plpy.execute(
            f"INSERT INTO {_quote(schema)}.{_quote(table)} SELECT * FROM ({query}) q"
        )
    return "CREATE TABLE"


# -----------------------------------------------------------------------------------------------
# ALTER TABLE
# -----------------------------------------------------------------------------------------------
_A_RENAME = re.compile(
    rf"^rename\s+(?:column\s+)?(?P<old>{_IDENT})\s+to\s+(?P<new>{_IDENT})$", re.I
)
_A_ADD_COLUMN = re.compile(
    rf"^add\s+(?:column\s+)?(?P<col>{_IDENT})\s+(?P<type>.+)$", re.I
)
_A_DROP_COLUMN = re.compile(rf"^drop\s+(?:column\s+)?(?P<col>{_IDENT})$", re.I)
_A_TYPE = re.compile(
    rf"^alter\s+(?:column\s+)?(?P<col>{_IDENT})\s+(?:set\s+data\s+)?type\s+(?P<type>.+)$",
    re.I,
)
_A_DEFAULT = re.compile(
    rf"^alter\s+(?:column\s+)?{_IDENT}\s+(?:set|drop)\s+default\b", re.I
)
_A_PROPERTIES = re.compile(
    r"^set\s+table\s+properties\s*\((?P<props>.*)\)$", re.I | re.S
)
_A_PARTITION = re.compile(
    r"^(?P<op>add|drop|replace)\s+partition\s+field\s+(?P<field>.+?)"
    r"(?:\s+with\s+(?P<new>.+))?$",
    re.I | re.S,
)


def _pg_type_of(plpy, type_sql: str) -> str:
    """Return ``format_type`` for a column type as written (int4, varchar, ...)."""
    plpy.execute(f"CREATE TEMP TABLE oblako_alter_type (c {type_sql}) ON COMMIT DROP")
    try:
        return _columns(plpy, "pg_temp.oblako_alter_type")[0][1]
    finally:
        plpy.execute("DROP TABLE pg_temp.oblako_alter_type")


def _widens(old, new) -> bool:
    """Whether Iceberg allows ``old`` -> ``new`` (int->long, float->double, decimal P)."""
    from pyiceberg import types as t

    if isinstance(old, t.IntegerType) and isinstance(new, t.LongType):
        return True
    if isinstance(old, t.FloatType) and isinstance(new, t.DoubleType):
        return True
    return (
        isinstance(old, t.DecimalType)
        and isinstance(new, t.DecimalType)
        and new.scale == old.scale
        and new.precision > old.precision
    )


def _spec_field(ice, item: str):
    """Find the partition field a PARTITIONED BY item names, or None."""
    ((col, fn, arg),) = parse_partitions(item)
    source = ice.schema().find_field(col).field_id
    want = str(_transform(fn, arg))
    for pf in ice.spec().fields:
        if pf.source_id == source and str(pf.transform) == want:
            return pf
    return None


def _drop_column(ice, database: str, table: str, col: str) -> None:
    """Drop a column, keeping the table's last column ID as Iceberg requires.

    pyiceberg sends a new schema without ``last-column-id``; the REST catalog then
    takes the new schema's highest ID, and dropping the newest column lowers it,
    which the catalog refuses. This commit carries the ID, or switches back to an
    identical earlier schema, as pyiceberg itself would.
    """
    import json
    import urllib.request
    from urllib.parse import quote

    update = ice.update_schema()
    update.delete_column(col)
    new_schema = update._apply()
    same = next((s.schema_id for s in ice.metadata.schemas if s == new_schema), None)
    if same is not None:
        updates = [{"action": "set-current-schema", "schema-id": same}]
    else:
        updates = [
            {
                "action": "add-schema",
                "schema": json.loads(new_schema.model_dump_json(by_alias=True)),
                "last-column-id": ice.metadata.last_column_id,
            },
            {"action": "set-current-schema", "schema-id": -1},
        ]
    body = {
        "requirements": [
            {
                "type": "assert-current-schema-id",
                "current-schema-id": ice.schema().schema_id,
            }
        ],
        "updates": updates,
    }
    request = urllib.request.Request(
        f"{iceberg_url()}/v1/namespaces/{quote(database, safe='')}"
        f"/tables/{quote(table, safe='')}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=30).close()


def _alter_table(plpy, name: str, action: str) -> str:
    """ALTER TABLE on an Iceberg table: a metadata change, then the view rebuilt."""
    parts = split_name(name)
    if len(parts) != 2:
        _fail(plpy, "name an Iceberg table <external_schema>.<table>")
    schema, table = parts
    reg = plpy.execute(
        "SELECT databasename, location FROM pg_oblako.iceberg_tables WHERE "
        f"schemaname = {plpy.quote_literal(schema)} AND "
        f"tablename = {plpy.quote_literal(table)}"
    )
    if not reg.nrows():
        _fail(plpy, f'relation "{schema}.{table}" does not exist')
    database, location = reg[0]["databasename"], reg[0]["location"]
    ice = _catalog_or_error(plpy).load_table((database, table))
    action = " ".join(action.split())
    partitioned = {pf.source_id for pf in ice.spec().fields}
    try:
        # before the column forms: ADD PARTITION FIELD x reads as ADD [COLUMN] too
        if m := _A_PARTITION.match(action):
            # validate everything first: pyiceberg commits an update on exit,
            # even one an error interrupted
            op, field = m.group("op").lower(), m.group("field")
            old = None
            if op in ("drop", "replace"):
                old = _spec_field(ice, field)
                if old is None:
                    _fail(plpy, f"partition field {field} is not in the table's spec")
            add = None
            if op in ("add", "replace"):
                item = m.group("new") if op == "replace" else field
                if not item:
                    _fail(plpy, "REPLACE PARTITION FIELD ... WITH ...")
                ((col, fn, arg),) = parse_partitions(item)
                source = ice.schema().find_field(col).field_id
                keep = partitioned - ({old.source_id} if old is not None else set())
                if source in keep:
                    _fail(
                        plpy,
                        f'"{col}"  used in multiple transform functions for '
                        '"iceberg" table',
                    )
                label = col if fn == "identity" else f"{col}_{fn}"
                add = (col, _transform(fn, arg), label)
            with ice.update_spec() as spec:
                if old is not None:
                    spec.remove_field(old.name)
                if add is not None:
                    spec.add_field(*add)
        elif m := _A_PROPERTIES.match(action):
            props = parse_properties(m.group("props"))
            if "compression_type" not in props:
                _fail(plpy, "SET TABLE PROPERTIES takes 'compression_type'")
            with ice.transaction() as tx:
                tx.set_properties(
                    {"write.parquet.compression-codec": props["compression_type"]}
                )
        elif m := _A_RENAME.match(action):
            old, new = split_name(m.group("old"))[0], split_name(m.group("new"))[0]
            with ice.update_schema() as update:
                update.rename_column(old, new)
        elif _A_DEFAULT.match(action) or re.search(r"(?i)\bdefault\b", action):
            _fail(
                plpy,
                'Columns constraints and attributes are not supported for an "iceberg" '
                'table. Default values are only supported with Iceberg version "3".',
            )
        elif m := _A_ADD_COLUMN.match(action):
            col, pgt = split_name(m.group("col"))[0], _pg_type_of(plpy, m.group("type"))
            if pgt.startswith("character varying("):
                _fail(
                    plpy,
                    f'VARCHAR(N) specifiying length is not supported for column "{col}" '
                    "in Iceberg table.",
                )
            with ice.update_schema() as update:
                update.add_column(col, iceberg_type(pgt))
        elif m := _A_DROP_COLUMN.match(action):
            col = split_name(m.group("col"))[0]
            field = ice.schema().find_field(col)
            if field.field_id in partitioned:
                _fail(
                    plpy,
                    f'column "{col}" belongs to the partition spec: drop its partition '
                    "field first",
                )
            _drop_column(ice, database, table, col)
        elif m := _A_TYPE.match(action):
            col = split_name(m.group("col"))[0]
            field = ice.schema().find_field(col)
            new = iceberg_type(_pg_type_of(plpy, m.group("type")))
            if field.field_id in partitioned:
                _fail(plpy, f'column "{col}" belongs to the partition spec')
            if not _widens(field.field_type, new):
                _fail(
                    plpy,
                    f"cannot change {field.field_type} to {new}: Iceberg widens only "
                    "int to bigint, float to double, and a decimal's precision",
                )
            with ice.update_schema() as update:
                update.update_column(col, field_type=new)
        else:
            _fail(plpy, f"ALTER TABLE {action} is not supported for Iceberg tables")
    except ValueError as e:
        _fail(plpy, str(e))
    # the view and staging table follow the new schema
    plpy.execute(f"DROP VIEW {_quote(schema)}.{_quote(table)}")
    ice = _catalog_or_error(plpy).load_table((database, table))
    _attach(plpy, schema, database, table, ice, location=location)
    return "ALTER TABLE"


# -----------------------------------------------------------------------------------------------
# MERGE
# -----------------------------------------------------------------------------------------------
_TARGET_ALIAS = re.compile(r"\s+(?:as\s+)?(?!using\b)([A-Za-z_][\w$]*)", re.IGNORECASE)


def _merge(plpy, stmt: str) -> str:
    """MERGE into an Iceberg table: the statement as written, run against a copy.

    The table's rows go into a temporary table, the MERGE runs there unchanged
    (every clause PostgreSQL's MERGE has), and the difference between copy and
    table is staged as deletes and inserts. The deferred commit then writes one
    snapshot, as any other write.
    """
    m = _MERGE.match(stmt)
    if m is None:
        _fail(plpy, "not a MERGE statement")
    parts = split_name(m.group("name"))
    if len(parts) < 2:
        _fail(plpy, "name the MERGE target <external_schema>.<table>")
    schema, table = parts[-2], parts[-1]
    reg = plpy.execute(
        "SELECT stage FROM pg_oblako.iceberg_tables WHERE "
        f"schemaname = {plpy.quote_literal(schema)} AND "
        f"tablename = {plpy.quote_literal(table)}"
    )
    if not reg.nrows():
        _fail(plpy, f'relation "{schema}.{table}" does not exist')
    stage = reg[0]["stage"]
    if plpy.execute(
        f"SELECT 1 FROM {stage} WHERE _stmt <> statement_timestamp() LIMIT 1"
    ).nrows():
        _fail(
            plpy,
            f"a transaction takes one write to Iceberg table {schema}.{table}, as on "
            "Redshift: COMMIT first",
        )
    view = f"{_quote(schema)}.{_quote(table)}"
    n = plpy.execute("SELECT nextval('pg_oblako.iceberg_stage_seq') AS n")[0]["n"]
    copy, gone, new = (f"oblako_merge_{n}{s}" for s in ("", "_d", "_i"))
    plpy.execute(f"CREATE TEMP TABLE {copy} AS SELECT * FROM {view}")
    for col in _not_null_columns(plpy, stage):
        plpy.execute(f"ALTER TABLE {copy} ALTER COLUMN {_quote(col)} SET NOT NULL")
    rest = m.group("rest")
    # the target keeps its name inside the statement (ON orders.id = s.id)
    target = f"pg_temp.{copy}"
    if not _TARGET_ALIAS.match(rest):
        target += f" AS {_quote(table)}"
    rest = re.sub(
        rf"(?i)\b{re.escape(schema)}\.{re.escape(table)}\.", f"{_quote(table)}.", rest
    )
    plpy.execute(f"MERGE INTO {target}{rest}")
    plpy.execute(
        f"CREATE TEMP TABLE {gone} AS SELECT * FROM {view} EXCEPT ALL "
        f"SELECT * FROM {copy}"
    )
    plpy.execute(
        f"CREATE TEMP TABLE {new} AS SELECT * FROM {copy} EXCEPT ALL "
        f"SELECT * FROM {view}"
    )
    plpy.execute(
        f"INSERT INTO {stage} SELECT 'D', statement_timestamp(), * FROM {gone}"
    )
    plpy.execute(f"INSERT INTO {stage} SELECT 'I', statement_timestamp(), * FROM {new}")
    plpy.execute(f"DROP TABLE {copy}, {gone}, {new}")
    return "MERGE"


# -----------------------------------------------------------------------------------------------
# Reads and commits
# -----------------------------------------------------------------------------------------------
def scan(database: str, name: str):
    """Every row of Iceberg table ``database.name``, as dicts for plpython."""
    rows = catalog().load_table((database, name)).scan().to_arrow().to_pylist()
    nested = None
    for row in rows:
        if nested is None:
            nested = [k for k, v in row.items() if isinstance(v, (dict, list))]
        for k in nested:
            if row[k] is not None:
                row[k] = json.dumps(row[k], default=str)
    return rows


def _arrow(rows, schema, oids):
    """Rows from plpython as an Arrow table with the Iceberg table's schema."""
    import pyarrow as pa
    from copy_unload import _from_pg

    arrow_schema = schema.as_arrow()
    names = [f.name for f in schema.fields]
    columns = [[_from_pg(r[n], oids[n]) for r in rows] for n in names]
    return pa.Table.from_arrays(
        [
            pa.array(col, type=arrow_schema.field(n).type)
            for col, n in zip(columns, names)
        ],
        schema=arrow_schema,
    )


def commit(plpy, stage: str) -> None:
    """Commit the staged writes of one Iceberg table: called once, at COMMIT."""
    rows = plpy.execute(
        "SELECT schemaname, tablename, databasename FROM pg_oblako.iceberg_tables "
        f"WHERE stage = {plpy.quote_literal(stage)}"
    )
    if not rows.nrows():
        return
    schema, name, database = (
        rows[0]["schemaname"],
        rows[0]["tablename"],
        rows[0]["databasename"],
    )
    table = _catalog_or_error(plpy).load_table((database, name))
    names = ", ".join(_quote(f.name) for f in table.schema().fields)
    deletes = plpy.execute(f"SELECT 1 FROM {stage} WHERE _op = 'D' LIMIT 1").nrows()
    if deletes:
        source = f"SELECT {names} FROM {_quote(schema)}.{_quote(name)}"
    else:
        source = f"SELECT {names} FROM {stage} WHERE _op = 'I'"
    res = plpy.execute(source)
    oids = dict(zip(res.colnames(), res.coltypes()))
    data = _arrow(list(res), table.schema(), oids)
    try:
        if deletes:
            table.overwrite(data)
        else:
            table.append(data)
    except Exception as e:  # a concurrent commit or the catalog's refusal
        plpy.error(f"committing to Iceberg table {database}.{name} failed: {e}")
    plpy.execute(f"DELETE FROM {stage}")


# -----------------------------------------------------------------------------------------------
# SHOW TABLE and DROP TABLE
# -----------------------------------------------------------------------------------------------
_TRANSFORM_SQL = re.compile(r"^(bucket|truncate)\[(\d+)\]$")
# the cast pg_get_expr adds to a default ('pending'::character varying)
_CAST_SUFFIX = re.compile(r"::[a-z ]+(\(\d+\))?$")


def show_table(plpy, name: str) -> str:
    """SHOW TABLE: the CREATE TABLE statement for a table (Iceberg or local)."""
    oid = plpy.execute(f"SELECT to_regclass({plpy.quote_literal(name)})::oid AS oid")[
        0
    ]["oid"]
    if oid is None:
        plpy.error(f'relation "{name}" does not exist')
    rel = plpy.execute(
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n "
        f"ON n.oid = c.relnamespace WHERE c.oid = {oid}"
    )[0]
    schema, table = rel["nspname"], rel["relname"]
    reg = plpy.execute(
        "SELECT databasename, location, stage FROM pg_oblako.iceberg_tables WHERE "
        f"schemaname = {plpy.quote_literal(schema)} AND "
        f"tablename = {plpy.quote_literal(table)}"
    )
    cols = plpy.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS t, a.attnotnull, "
        "pg_get_expr(d.adbin, d.adrelid) AS dflt FROM pg_attribute a "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        f"WHERE a.attrelid = {oid} AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum"
    )
    # an Iceberg table's view carries no NOT NULL; its staging table does
    required = _not_null_columns(plpy, reg[0]["stage"]) if reg.nrows() else set()
    lines = []
    for c in cols:
        line = f"{c['attname']} {redshift_type(c['t'])}"
        if c["dflt"] is not None:
            default = _CAST_SUFFIX.sub("", c["dflt"])
            line += f" DEFAULT {default}"
        if c["attnotnull"] or c["attname"] in required:
            line += " NOT NULL"
        lines.append(line)
    # Redshift's layout: one column per line, the list closed on the last one
    ddl = f"CREATE TABLE {schema}.{table} (" + ",\n".join(lines) + ")"
    if not reg.nrows():
        return ddl + ";"
    ice = _catalog_or_error(plpy).load_table((reg[0]["databasename"], table))
    ddl += f"\nUSING ICEBERG\nLOCATION '{reg[0]['location'].rstrip('/')}'"
    field_names = {f.field_id: f.name for f in ice.schema().fields}
    parts = []
    for pf in ice.spec().fields:
        col = field_names[pf.source_id]
        t = str(pf.transform)
        if t == "identity":
            parts.append(col)
        elif m := _TRANSFORM_SQL.match(t):
            parts.append(f"{m.group(1).upper()}({m.group(2)}, {col})")
        else:
            parts.append(f"{t.upper()}({col})")
    if parts:
        ddl += f"\nPARTITIONED BY ({', '.join(parts)})"
    codec = ice.properties.get("write.parquet.compression-codec", "zstd")
    ddl += (
        f"\nTABLE PROPERTIES ('format-version'='{ice.metadata.format_version}', "
        f"'compression_type'='{codec}')"
    )
    return ddl + ";"


_REDSHIFT_TYPES = {
    "integer": "int",
    "character varying": "varchar",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
    "bytea": "varbyte",
}


def redshift_type(pg: str) -> str:
    """Return the name Redshift's SHOW TABLE gives a PostgreSQL column type."""
    if m := re.fullmatch(r"numeric\((\d+),(\d+)\)", pg):
        return f"decimal({m.group(1)}, {m.group(2)})"
    if m := re.fullmatch(r"character varying\((\d+)\)", pg):
        return f"varchar({m.group(1)})"
    if m := re.fullmatch(r"character\((\d+)\)", pg):
        return f"char({m.group(1)})"
    return _REDSHIFT_TYPES.get(pg, pg)


_DROP_TABLE = re.compile(
    r"(?is)\bdrop\s+table\s+(?:if\s+exists\s+)?(?P<names>.+?)\s*(?:\bcascade\b|\brestrict\b|;|$)"
)


def _on_drop_table(plpy, query: str | None) -> None:
    """Before DROP TABLE: drop named Iceberg tables from the catalog (files stay).

    The view is swapped for an empty table of the same name, which the DROP TABLE
    then drops, so the statement completes as DROP TABLE does on Redshift.
    """
    for m in _DROP_TABLE.finditer(query or ""):
        for raw in re.findall(rf"{_NAME}", m.group("names")):
            row = plpy.execute(
                "SELECT t.schemaname, t.tablename, t.databasename "
                "FROM pg_oblako.iceberg_tables t JOIN pg_class c "
                "ON c.relname = t.tablename JOIN pg_namespace n "
                "ON n.oid = c.relnamespace AND n.nspname = t.schemaname "
                f"WHERE c.oid = to_regclass({plpy.quote_literal(raw)})"
            )
            if not row.nrows():
                continue
            schema, table, database = (
                row[0]["schemaname"],
                row[0]["tablename"],
                row[0]["databasename"],
            )
            try:
                _catalog_or_error(plpy).drop_table((database, table))
            except Exception as e:  # already gone from the catalog is fine
                plpy.notice(f"Iceberg table {database}.{table}: {e}")
            view = f"{_quote(schema)}.{_quote(table)}"
            plpy.execute(f"DROP VIEW {view}")
            plpy.execute(f"CREATE TABLE {view} ()")


# -----------------------------------------------------------------------------------------------
# Entry points (the plpython3u functions in 13_iceberg.sql)
# -----------------------------------------------------------------------------------------------
def create_external_schema(plpy, *args) -> str:
    """CREATE EXTERNAL SCHEMA ... FROM DATA CATALOG (see ``_create_external_schema``)."""
    with _coordinator_only(plpy):
        return _create_external_schema(plpy, *args)


def create_table(plpy, *args) -> str:
    """CREATE TABLE ... USING ICEBERG (see ``_create_table``)."""
    with _coordinator_only(plpy):
        return _create_table(plpy, *args)


def on_drop_table(plpy, query: str | None) -> None:
    """Before DROP TABLE (see ``_on_drop_table``)."""
    with _coordinator_only(plpy):
        _on_drop_table(plpy, query)


def merge(plpy, stmt: str) -> str:
    """MERGE into an Iceberg table (see ``_merge``)."""
    with _coordinator_only(plpy):
        return _merge(plpy, stmt)


def alter_table(plpy, name: str, action: str) -> str:
    """ALTER TABLE on an Iceberg table (see ``_alter_table``)."""
    with _coordinator_only(plpy):
        return _alter_table(plpy, name, action)
