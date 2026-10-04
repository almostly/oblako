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


def rewrite_iceberg(sql: str) -> str:
    """Rewrite external-schema and Iceberg DDL into pg_oblako function calls."""
    if m := _EXTERNAL_SCHEMA.match(sql):
        db = _DATABASE.search(m.group("rest"))
        database = db.group(1).replace("''", "'") if db else None
        return (
            "SELECT pg_oblako.create_external_schema("
            f"{_literal(m.group('schema'))}, {_literal(database)}, "
            f"{'true' if _CREATE_DB.search(m.group('rest')) else 'false'}, "
            f"{'true' if m.group('ine') else 'false'}) AS status"
        )
    if (p := parse_create_iceberg(sql)) is not None:
        return (
            "SELECT pg_oblako.iceberg_create_table("
            f"{_literal(p['name'])}, {_literal(p['columns'])}, "
            f"{_literal(p['location'])}, {_literal(p['partitioned'])}, "
            f"{_literal(p['properties'])}, "
            f"{'true' if p['if_not_exists'] else 'false'}, {_literal(p['query'])}"
            ") AS status"
        )
    if m := _SHOW_TABLE.match(sql):
        return (
            f"SELECT pg_oblako.show_table({_literal(m.group('name'))}) "
            'AS "Show Table DDL statement"'
        )
    return sql


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
        if key == "format-version":
            if value not in ("2", "3"):
                raise ValueError(f"format-version must be '2' or '3', not '{value}'")
        elif key == "compression_type":
            value = value.lower()
            if value not in _COMPRESSIONS:
                raise ValueError(
                    f"compression_type must be one of {', '.join(sorted(_COMPRESSIONS))}"
                )
        else:
            raise ValueError(
                f"unsupported table property '{key}': Iceberg tables take "
                "'format-version' and 'compression_type'"
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
            raise ValueError(f"column {c} is used in more than one partition transform")
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
        return "character varying(65535)"
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
            cols.append(f"{_quote(field.name)} {pgt}")
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
    plpy.execute(
        f"CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $fn$\n"
        "BEGIN\n"
        f"  IF EXISTS (SELECT 1 FROM {stage} WHERE _stmt < statement_timestamp()\n"
        "             OR _stmt > statement_timestamp()) THEN\n"
        "    RAISE EXCEPTION 'a transaction takes one write to Iceberg table %, as on "
        f"Redshift: COMMIT first', {plpy.quote_literal(schema + '.' + name)};\n"
        "  END IF;\n"
        "  IF TG_OP <> 'INSERT' THEN\n"
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
    if not location:
        _fail(
            plpy, "CREATE TABLE ... USING ICEBERG in an external schema needs LOCATION"
        )
    try:
        if not _location_is_empty(location):
            plpy.error(f"LOCATION {location} is not empty")
    except ValueError as e:
        plpy.error(str(e))
    version = props.get("format-version", "2")
    if version == "3":
        # the REST catalog oblako runs writes v2 metadata only
        plpy.error(
            "Iceberg v3 tables ('format-version'='3') are not supported in oblako "
            "yet; leave format-version out for a v2 table"
        )
    if columns and re.search(r"(?i)\bdefault\b", columns):
        plpy.error(
            "column DEFAULT values need an Iceberg v3 table: 'format-version'='3'"
        )
    if columns and re.search(
        r"(?i)\b(primary\s+key|unique|references|not\s+null|identity|encode|"
        r"distkey|sortkey|collate)\b",
        columns,
    ):
        plpy.error("Iceberg tables take no column constraints or attributes")

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
    fields = []
    try:
        for i, (col, typ) in enumerate(_columns(plpy, stage), start=1):
            fields.append(NestedField(i, col, iceberg_type(typ), required=False))
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
        "SELECT databasename, location FROM pg_oblako.iceberg_tables WHERE "
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
    lines = []
    for c in cols:
        line = f"{c['attname']} {c['t']}"
        if c["dflt"] is not None:
            default = _CAST_SUFFIX.sub("", c["dflt"])
            line += f" DEFAULT {default}"
        if c["attnotnull"]:
            line += " NOT NULL"
        lines.append(line)
    ddl = f"CREATE TABLE {schema}.{table} (\n  " + ",\n  ".join(lines) + "\n)"
    if not reg.nrows():
        return ddl + ";"
    ice = _catalog_or_error(plpy).load_table((reg[0]["databasename"], table))
    ddl += f"\nUSING ICEBERG\nLOCATION '{reg[0]['location']}'"
    field_names = {f.field_id: f.name for f in ice.schema().fields}
    parts = []
    for pf in ice.spec().fields:
        col = field_names[pf.source_id]
        t = str(pf.transform)
        if t == "identity":
            parts.append(col)
        elif m := _TRANSFORM_SQL.match(t):
            parts.append(f"{m.group(1)}({m.group(2)}, {col})")
        else:
            parts.append(f"{t}({col})")
    if parts:
        ddl += f"\nPARTITIONED BY ({', '.join(parts)})"
    props = []
    if ice.metadata.format_version == 3:
        props.append("'format-version'='3'")
    codec = ice.properties.get("write.parquet.compression-codec", "zstd")
    if codec != "zstd":
        props.append(f"'compression_type'='{codec}'")
    if props:
        ddl += f"\nTABLE PROPERTIES ({', '.join(props)})"
    return ddl + ";"


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
