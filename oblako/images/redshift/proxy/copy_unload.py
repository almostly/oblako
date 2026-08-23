"""Bridge Redshift ``COPY``/``UNLOAD`` (to/from ``s3://``) to an S3 object store.

Redshift's ``COPY t FROM 's3://...'`` and ``UNLOAD ('select') TO 's3://...'`` have
no PostgreSQL equivalent (native COPY only reads local files / STDIN, and there is
no UNLOAD), so PostgreSQL rejects them at parse time. Rather than teach the wire
proxy to synthesize protocol responses, the proxy simply **rewrites** each such
statement into a call to a ``plpython3u`` function that does the object-store work
(``rewrite_copy_unload``):

    COPY t FROM 's3://b/k' ... FORMAT AS PARQUET
        -> SELECT oblako_copy_from_s3('t', 's3://b/k', NULL, 'PARQUET', '{...}')
    UNLOAD ('select ...') TO 's3://b/p/' ... FORMAT AS PARQUET
        -> SELECT oblako_unload_to_s3('select ...', 's3://b/p/', 'PARQUET', '{...}')

PostgreSQL then runs the rewritten statement normally, so this works the same for
the simple ('Q') and extended ('P'/'B'/'E') protocols, and because the function
runs in-session (SPI) it sees temporary tables created earlier in the same batch
(the redshift-data engine's ``CREATE TEMP TABLE ...; UNLOAD(...)`` pattern).

Formats: ``PARQUET`` (default when a client says so; via pyarrow), ``CSV``, and the
default delimited ``TEXT`` (pipe) format; the last two via the stdlib ``csv`` with
``DELIMITER`` / ``HEADER`` / ``IGNOREHEADER`` / ``NULL AS`` / ``QUOTE`` options.

Self-contained (no ``oblako`` imports) so it can be copied into the redshift image
next to ``redshift_proxy.py`` and imported both by the proxy (``rewrite_*``,
parsing) and by the in-engine plpython functions (``do_copy`` / ``do_unload``).
``pydantic`` is needed to import the module; ``boto3`` / ``pyarrow`` are imported
lazily, only inside the execution functions.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import os
import re

from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# SQL detection / parsing (proxy side)
# ---------------------------------------------------------------------------
_S3_COPY = re.compile(
    r"^\s*COPY\s+(?P<table>[A-Za-z0-9_.\"]+)\s*(?:\((?P<cols>[^)]*)\))?"
    r"\s+FROM\s+'(?P<uri>s3://[^']+)'\s*(?P<opts>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_S3_UNLOAD_HEAD = re.compile(r"^\s*UNLOAD\s*\(", re.IGNORECASE)

_DELIMITER = re.compile(r"(?i)\bdelimiter\s+(?:as\s+)?'([^']*)'")
_NULL_AS = re.compile(r"(?i)\bnull\s+as\s+'([^']*)'")
_QUOTE = re.compile(r"(?i)\bquote\s+(?:as\s+)?'([^']*)'")
_IGNOREHEADER = re.compile(r"(?i)\bignoreheader\s+(?:as\s+)?(\d+)")
_HEADER = re.compile(r"(?i)\bheader\b")
_ADDQUOTES = re.compile(r"(?i)\baddquotes\b")


def has_s3_copy_or_unload(sql: str) -> bool:
    """Cheap gate: does this SQL carry a COPY-from-s3 or UNLOAD-to-s3 anywhere?"""
    if "s3://" not in sql:
        return False
    upper = sql.upper()
    return "COPY" in upper or "UNLOAD" in upper


def split_statements(sql: str) -> list[str]:
    """Split a multi-statement string on ``;``, respecting single-quoted literals."""
    stmts: list[str] = []
    buf: list[str] = []
    i, n, in_str = 0, len(sql), False
    while i < n:
        c = sql[i]
        if in_str:
            buf.append(c)
            if c == "'":
                if i + 1 < n and sql[i + 1] == "'":  # doubled quote -> literal '
                    buf.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            buf.append(c)
        elif c == ";":
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
        else:
            buf.append(c)
        i += 1
    s = "".join(buf).strip()
    if s:
        stmts.append(s)
    return stmts


def _read_sql_string(text: str, start: int) -> tuple[str, int]:
    """Parse a single-quoted SQL literal at ``text[start]`` ('), unescaping ``''``."""
    assert text[start] == "'"
    out: list[str] = []
    i = start + 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "'":
            if i + 1 < n and text[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise ValueError("unterminated string literal")


def _bucket_key(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/key/parts`` into ``(bucket, key)``."""
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    return bucket, key


_SUPPORTED_FORMATS = ("PARQUET", "CSV", "TEXT")
_UNSUPPORTED_FORMAT = re.compile(r"(?i)\b(?:format\s+(?:as\s+)?)?(json|avro|orc)\b")


def _detect_format(opts: str) -> str:
    """PARQUET / CSV / TEXT from a COPY/UNLOAD options tail.

    A recognised-but-unsupported binary format (JSON/AVRO/ORC) is returned as-is
    so the engine function raises a clear error rather than mis-reading it as text.
    """
    if "PARQUET" in opts.upper():
        return "PARQUET"
    if re.search(r"(?i)\bcsv\b", opts):
        return "CSV"
    other = _UNSUPPORTED_FORMAT.search(opts)
    if other:
        return other.group(1).upper()
    return "TEXT"


def _unescape_delim(d: str) -> str:
    """Turn a literal ``\\t`` in a DELIMITER value into a real tab."""
    return d.encode().decode("unicode_escape") if "\\" in d else d


class CopyCommand(BaseModel):
    """A parsed ``COPY <table> [(cols)] FROM 's3://...' ...`` statement."""

    table: str
    columns: list[str] | None = None
    uri: str
    fmt: str = "PARQUET"
    delimiter: str | None = None
    null_as: str | None = None
    quote: str | None = None
    ignore_header: int = 0

    @field_validator("fmt")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def options(self) -> dict:
        """The non-parquet read options, as passed to the engine function."""
        return {
            "delimiter": self.delimiter,
            "null_as": self.null_as,
            "quote": self.quote,
            "ignore_header": self.ignore_header,
        }


class UnloadCommand(BaseModel):
    """A parsed ``UNLOAD ('query') TO 's3://prefix/' ...`` statement.

    Redshift defaults to ``PARALLEL ON`` (one part-file per slice); ``PARALLEL
    OFF`` writes one file. redshift-local is a single slice, so one part-file is a
    correct result either way. ``PARALLEL`` / ``ALLOWOVERWRITE`` / ``MAXFILESIZE``
    are accepted and ignored.
    """

    query: str
    uri: str
    fmt: str = "PARQUET"
    delimiter: str | None = None
    null_as: str | None = None
    quote: str | None = None
    header: bool = False
    addquotes: bool = False

    @field_validator("fmt")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def options(self) -> dict:
        """The non-parquet write options, as passed to the engine function."""
        return {
            "delimiter": self.delimiter,
            "null_as": self.null_as,
            "quote": self.quote,
            "header": self.header,
            "addquotes": self.addquotes,
        }


def parse_copy(stmt: str) -> CopyCommand | None:
    """Parse a COPY-from-s3 statement into a ``CopyCommand`` (or None)."""
    m = _S3_COPY.match(stmt)
    if not m:
        return None
    opts = m.group("opts") or ""
    delim = _DELIMITER.search(opts)
    nullas = _NULL_AS.search(opts)
    quote = _QUOTE.search(opts)
    ihdr = _IGNOREHEADER.search(opts)
    cols = [c.strip() for c in m.group("cols").split(",")] if m.group("cols") else None
    return CopyCommand(
        table=m.group("table"),
        columns=cols,
        uri=m.group("uri"),
        fmt=_detect_format(opts),
        delimiter=_unescape_delim(delim.group(1)) if delim else None,
        null_as=nullas.group(1) if nullas else None,
        quote=quote.group(1) if quote else None,
        ignore_header=int(ihdr.group(1)) if ihdr else 0,
    )


def parse_unload(stmt: str) -> UnloadCommand | None:
    """Parse an UNLOAD-to-s3 statement into an ``UnloadCommand`` (or None)."""
    if not (_S3_UNLOAD_HEAD.match(stmt) and "s3://" in stmt):
        return None
    open_paren = stmt.index("(", stmt.upper().index("UNLOAD"))
    q_start = stmt.index("'", open_paren)
    query, after = _read_sql_string(stmt, q_start)
    to_idx = stmt.upper().index("TO", after)
    uri_start = stmt.index("'", to_idx)
    uri, tail_at = _read_sql_string(stmt, uri_start)
    opts = stmt[tail_at:]
    delim = _DELIMITER.search(opts)
    nullas = _NULL_AS.search(opts)
    quote = _QUOTE.search(opts)
    return UnloadCommand(
        query=query,
        uri=uri,
        fmt=_detect_format(opts),
        delimiter=_unescape_delim(delim.group(1)) if delim else None,
        null_as=nullas.group(1) if nullas else None,
        quote=quote.group(1) if quote else None,
        header=bool(_HEADER.search(opts)),
        addquotes=bool(_ADDQUOTES.search(opts)),
    )


def _dollar_quote(s: str) -> str:
    """Wrap ``s`` in a dollar-quoted literal with a tag guaranteed not to occur."""
    tag = "$ob$"
    if tag in s:
        n = 0
        while f"$ob{n}$" in s:
            n += 1
        tag = f"$ob{n}$"
    return f"{tag}{s}{tag}"


def rewrite_copy_unload(sql: str) -> str:
    """Rewrite COPY-from-s3 / UNLOAD-to-s3 statements into oblako_* function calls.

    Non-COPY/UNLOAD statements in the batch are kept verbatim, so a mixed batch
    like ``CREATE TEMP TABLE ...; UNLOAD(...)`` keeps the temp-table create and
    only rewrites the UNLOAD. Returns ``sql`` unchanged when there's nothing to do.
    """
    if not has_s3_copy_or_unload(sql):
        return sql
    out: list[str] = []
    for stmt in split_statements(sql):
        unload = parse_unload(stmt)
        copy = None if unload else parse_copy(stmt)
        if unload is not None:
            opts = _dollar_quote(json.dumps(unload.options()))
            out.append(
                "SELECT oblako_unload_to_s3("
                f"{_dollar_quote(unload.query)}, {_dollar_quote(unload.uri)}, "
                f"{_dollar_quote(unload.fmt)}, {opts})"
            )
        elif copy is not None:
            if copy.columns:
                cols = (
                    "ARRAY[" + ", ".join(_dollar_quote(c) for c in copy.columns) + "]"
                )
            else:
                cols = "NULL::text[]"
            opts = _dollar_quote(json.dumps(copy.options()))
            out.append(
                "SELECT oblako_copy_from_s3("
                f"{_dollar_quote(copy.table)}, {_dollar_quote(copy.uri)}, {cols}, "
                f"{_dollar_quote(copy.fmt)}, {opts})"
            )
        else:
            out.append(stmt)
    return "; ".join(out)


# ---------------------------------------------------------------------------
# Execution (engine side: called by the plpython3u oblako_* functions)
# ---------------------------------------------------------------------------
# PostgreSQL type OIDs whose plpython text form needs parsing back to a real
# Python temporal type (plpython hands these over as strings).
_OID_TIMESTAMP = 1114
_OID_TIMESTAMPTZ = 1184
_OID_DATE = 1082
_OID_TIME = 1083
_OID_TIMETZ = 1266


def s3_client():
    """Build an S3 client for the local object store (S3Proxy).

    The endpoint comes from ``OBLAKO_S3_ENDPOINT`` / ``AWS_ENDPOINT_URL_S3`` /
    ``AWS_ENDPOINT_URL``; if none is set it falls back to real AWS (so a genuine
    ``s3://`` with real credentials also works). Path-style addressing is forced
    (S3Proxy can't resolve virtual-host ``bucket.host``) and checksums are only
    sent when required (S3Proxy lacks SDK-v2 flexible checksums).
    """
    import boto3
    from botocore.config import Config

    endpoint = (
        os.environ.get("OBLAKO_S3_ENDPOINT")
        or os.environ.get("AWS_ENDPOINT_URL_S3")
        or os.environ.get("AWS_ENDPOINT_URL")
    )
    try:
        cfg = Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        )
    except TypeError:  # older botocore without the checksum knob
        cfg = Config(s3={"addressing_style": "path"})
    kwargs: dict = {
        "config": cfg,
        "region_name": os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or "us-east-1",
    }
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if endpoint and not os.environ.get("AWS_ACCESS_KEY_ID"):
        # S3Proxy runs with auth disabled; any credentials satisfy botocore.
        kwargs["aws_access_key_id"] = "oblako"
        kwargs["aws_secret_access_key"] = "oblako"
    return boto3.client("s3", **kwargs)


def _from_pg(value, oid: int):
    """Convert a plpython value to a real Python type when it's a temporal string."""
    if value is None or not isinstance(value, str):
        return value
    if oid in (_OID_TIMESTAMP, _OID_TIMESTAMPTZ):
        return _dt.datetime.fromisoformat(value)
    if oid == _OID_DATE:
        return _dt.date.fromisoformat(value)
    if oid in (_OID_TIME, _OID_TIMETZ):
        return _dt.time.fromisoformat(value)
    return value


def _delimiter_for(fmt: str, opts: dict) -> str:
    """The field delimiter: the given one, else ',' for CSV and '|' for TEXT."""
    if opts.get("delimiter"):
        return opts["delimiter"]
    return "," if fmt == "CSV" else "|"


def _s3_object_keys(s3, bucket: str, prefix: str) -> list[str]:
    """Every object key under an s3 prefix (a single key matches itself)."""
    return [
        o["Key"]
        for o in s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    ]


def do_unload(
    plpy, query: str, uri: str, fmt: str = "PARQUET", opts: str = "{}"
) -> int:
    """Run ``query`` and write its result to ``uri``. Returns the row count.

    Runs via SPI on the caller's session, so temp tables created earlier in the
    same batch are visible. Called by the ``oblako_unload_to_s3`` plpython3u UDF.
    """
    fmt = fmt.upper()
    if fmt not in _SUPPORTED_FORMATS:
        plpy.error(f"unsupported UNLOAD format {fmt}; use PARQUET, CSV, or text")
    options = json.loads(opts) if opts else {}
    bucket, key = _bucket_key(uri)
    key = key.rstrip("/")
    res = plpy.execute(query)
    names = list(res.colnames())
    oids = list(res.coltypes())
    nrows = res.nrows()

    if fmt == "PARQUET":
        import pyarrow as pa
        import pyarrow.parquet as pq

        columns = {
            name: [_from_pg(res[r][name], oids[i]) for r in range(nrows)]
            for i, name in enumerate(names)
        }
        table = pa.table(columns) if names else pa.table({})
        buf = io.BytesIO()
        pq.write_table(
            table, buf, coerce_timestamps="us", allow_truncated_timestamps=True
        )
        body, ext = buf.getvalue(), ".parquet"
    else:
        # CSV / default TEXT: delimited, plpython's text forms are already what we
        # want to write; None -> the NULL sentinel (default empty).
        delim = _delimiter_for(fmt, options)
        null_as = options.get("null_as") or ""
        sio = io.StringIO()
        writer = csv.writer(
            sio,
            delimiter=delim,
            quotechar=options.get("quote") or '"',
            quoting=csv.QUOTE_ALL if options.get("addquotes") else csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        if options.get("header"):
            writer.writerow(names)
        for r in range(nrows):
            writer.writerow(
                [null_as if res[r][c] is None else _csv_cell(res[r][c]) for c in names]
            )
        body, ext = sio.getvalue().encode("utf-8"), ".csv" if fmt == "CSV" else ""

    s3_client().put_object(Bucket=bucket, Key=f"{key}/0000_part_00{ext}", Body=body)
    return nrows


def _csv_cell(value) -> str:
    """Render a plpython value for a delimited cell (bool -> true/false)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _target_columns(plpy, table: str) -> list[str]:
    """The table's columns in definition order (for positional CSV/TEXT COPY)."""
    q = plpy.execute(
        "SELECT attname FROM pg_attribute "
        f"WHERE attrelid = {plpy.quote_literal(table)}::regclass "
        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
    )
    return [r["attname"] for r in q]


def _target_column_types(plpy, table: str, columns: list[str]) -> list[str]:
    """The PostgreSQL type of each target column, for a typed prepared INSERT."""
    q = plpy.execute(
        "SELECT attname, format_type(atttypid, atttypmod) AS t "
        f"FROM pg_attribute WHERE attrelid = {plpy.quote_literal(table)}::regclass "
        "AND attnum > 0 AND NOT attisdropped"
    )
    by_name = {r["attname"]: r["t"] for r in q}
    return [by_name.get(c, "text") for c in columns]


def _insert_rows(plpy, table: str, columns: list[str], rows) -> int:
    """Insert rows (lists aligned to ``columns``) with a typed prepared plan.

    Values bound for a JSON/SUPER (jsonb-backed) column that arrive as a Python
    dict/list (nested Parquet) are serialized to JSON text so the jsonb input
    accepts them; CSV values are already text and pass through.
    """
    types = _target_column_types(plpy, table, columns)
    json_idx = [i for i, t in enumerate(types) if t in ("jsonb", "json", "super")]
    collist = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
    plan = plpy.prepare(
        f"INSERT INTO {table} ({collist}) VALUES ({placeholders})", types
    )
    count = 0
    for row in rows:
        row = list(row)
        for i in json_idx:
            if row[i] is not None and not isinstance(row[i], str):
                row[i] = json.dumps(row[i], default=str)
        plpy.execute(plan, row)
        count += 1
    return count


def do_copy(
    plpy, table: str, uri: str, columns=None, fmt: str = "PARQUET", opts: str = "{}"
) -> int:
    """Load object(s) under ``uri`` into ``table``. Returns rows inserted.

    Reads every object under the s3 prefix (a single key or a directory both
    work). Parquet maps columns by name; CSV/TEXT map by position (a column list
    or the table's definition order). Called by the ``oblako_copy_from_s3`` UDF.
    """
    fmt = fmt.upper()
    if fmt not in _SUPPORTED_FORMATS:
        plpy.error(f"unsupported COPY format {fmt}; use PARQUET, CSV, or text")
    options = json.loads(opts) if opts else {}
    bucket, key = _bucket_key(uri)
    s3 = s3_client()
    keys = _s3_object_keys(s3, bucket, key)
    if not keys:
        plpy.error(f"no S3 objects found under s3://{bucket}/{key}")

    if fmt == "PARQUET":
        import pyarrow as pa
        import pyarrow.parquet as pq

        tables = [
            pq.read_table(
                io.BytesIO(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
            )
            for k in keys
        ]
        data = pa.concat_tables(tables) if len(tables) > 1 else tables[0]
        target_cols = list(columns) if columns else list(data.column_names)
        source = [data.column(name).to_pylist() for name in data.column_names]
        return _insert_rows(plpy, table, target_cols, zip(*source))

    # CSV / default TEXT: positional load. An empty field or the NULL sentinel
    # becomes NULL (Redshift's default for a delimited empty field).
    delim = _delimiter_for(fmt, options)
    null_as = options.get("null_as")
    ignore = int(options.get("ignore_header") or 0)
    quote = options.get("quote") or '"'
    target_cols = list(columns) if columns else _target_columns(plpy, table)
    rows: list[list] = []
    for k in keys:
        text = s3.get_object(Bucket=bucket, Key=k)["Body"].read().decode("utf-8")
        reader = csv.reader(io.StringIO(text), delimiter=delim, quotechar=quote)
        file_rows = list(reader)[ignore:]
        for raw in file_rows:
            rows.append([None if (v == "" or v == null_as) else v for v in raw])
    return _insert_rows(plpy, table, target_cols, rows)
