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

Formats: ``PARQUET`` (default when a client says so; via pyarrow), ``CSV``, the
default delimited ``TEXT`` (pipe) format via the stdlib ``csv`` (with ``DELIMITER``
/ ``HEADER`` / ``IGNOREHEADER`` / ``NULL AS`` / ``QUOTE`` options), and ``JSON``
(``FORMAT AS JSON 'auto' | 'auto ignorecase' | 'noshred' | 's3://.../paths.json'``)
which shreds into columns incl. SUPER. ``GZIP`` / ``BZIP2`` / ``ZSTD`` compression
is decompressed on read (gzip is also auto-detected by magic bytes).

Self-contained (no ``oblako`` imports) so it can be copied into the redshift image
next to ``redshift_proxy.py`` and imported both by the proxy (``rewrite_*``,
parsing) and by the in-engine plpython functions (``do_copy`` / ``do_unload``).
``pydantic`` is needed to import the module; ``boto3`` / ``pyarrow`` are imported
lazily, only inside the execution functions.
"""

from __future__ import annotations

import csv
import decimal
import datetime as _dt
import io
import json
import os
import re
import urllib.parse

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
_ALLOWOVERWRITE = re.compile(r"(?i)\ballowoverwrite\b")
_CLEANPATH = re.compile(r"(?i)\bcleanpath\b")
_PARTITION_BY = re.compile(r"(?i)\bpartition\s+by\s*\(([^)]*)\)\s*(include\b)?")
_MANIFEST = re.compile(r"(?i)\bmanifest\b(\s+verbose\b)?")
_EXTENSION = re.compile(r"(?i)\bextension\s+'([^']*)'")
_PARALLEL_OFF = re.compile(r"(?i)\bparallel\s+(?:off|false)\b")


def has_s3_copy_or_unload(sql: str) -> bool:
    """Return whether this SQL carries a COPY from S3 or an UNLOAD to S3 (a cheap gate)."""
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


_SUPPORTED_FORMATS = ("PARQUET", "CSV", "TEXT", "JSON")
_UNSUPPORTED_FORMAT = re.compile(r"(?i)\b(avro|orc)\b")
# [FORMAT [AS]] JSON [ 'auto' | 'auto ignorecase' | 'noshred' | 's3://.../paths.json' ]
# The `FORMAT AS` is optional (Redshift accepts a bare `JSON 'auto'`); the bare form
# needs the quoted arg so it can't match a `.json` filename elsewhere in the tail.
_JSON_FMT = re.compile(r"(?i)\bformat\s+(?:as\s+)?json\b|\bjson\s+'")
_JSON_ARG = re.compile(r"(?i)(?:\bformat\s+(?:as\s+)?)?json\s+'([^']*)'")
_COMPRESSION = re.compile(r"(?i)\b(gzip|bzip2|zstd)\b")


def _detect_format(opts: str) -> str:
    """PARQUET / CSV / TEXT / JSON from a COPY/UNLOAD options tail.

    A recognised-but-unsupported binary format (AVRO/ORC) is returned as-is so the
    engine function raises a clear error rather than mis-reading it as text.
    """
    if "PARQUET" in opts.upper():
        return "PARQUET"
    if _JSON_FMT.search(opts):
        return "JSON"
    if re.search(r"(?i)\bcsv\b", opts):
        return "CSV"
    other = _UNSUPPORTED_FORMAT.search(opts)
    if other:
        return other.group(1).upper()
    return "TEXT"


def _unescape_delim(d: str) -> str:
    r"""Turn a literal ``\t`` in a DELIMITER value into a real tab."""
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
    json_arg: str | None = (
        None  # 'auto' | 'auto ignorecase' | 'noshred' | jsonpaths s3 uri
    )
    compression: str | None = None  # gzip | bzip2 | zstd

    @field_validator("fmt")
    @classmethod
    def _upper(cls, v: str) -> str:
        """Upper-case the format name."""
        return v.upper()

    def options(self) -> dict:
        """Return the non-parquet read options, as passed to the engine function."""
        return {
            "delimiter": self.delimiter,
            "null_as": self.null_as,
            "quote": self.quote,
            "ignore_header": self.ignore_header,
            "json_arg": self.json_arg,
            "compression": self.compression,
        }


class UnloadCommand(BaseModel):
    """A parsed ``UNLOAD ('query') TO 's3://prefix/' ...`` statement.

    Redshift defaults to ``PARALLEL ON`` (one part-file per slice); ``PARALLEL
    OFF`` writes one file. redshift-local is a single slice, so one part-file is a
    correct result either way, named as Redshift names a parallel or serial
    unload. ``MAXFILESIZE`` is accepted and ignored. As on Redshift, UNLOAD into a
    prefix that already holds files fails unless ``ALLOWOVERWRITE`` (overwrite) or
    ``CLEANPATH`` (remove them first); ``PARTITION BY`` writes Hive-style folders and
    ``MANIFEST`` a JSON list of the files.
    """

    query: str
    uri: str
    fmt: str = "PARQUET"
    delimiter: str | None = None
    null_as: str | None = None
    quote: str | None = None
    header: bool = False
    addquotes: bool = False
    allowoverwrite: bool = False
    cleanpath: bool = False
    partition_by: list[str] = []
    include: bool = False
    manifest: bool = False
    verbose: bool = False
    extension: str | None = None
    compression: str | None = None
    parallel: bool = True

    @field_validator("fmt")
    @classmethod
    def _upper(cls, v: str) -> str:
        """Upper-case the format name."""
        return v.upper()

    def options(self) -> dict:
        """Return the non-parquet write options, as passed to the engine function."""
        return {
            "delimiter": self.delimiter,
            "null_as": self.null_as,
            "quote": self.quote,
            "header": self.header,
            "addquotes": self.addquotes,
            "allowoverwrite": self.allowoverwrite,
            "cleanpath": self.cleanpath,
            "partition_by": self.partition_by,
            "include": self.include,
            "manifest": self.manifest,
            "verbose": self.verbose,
            "extension": self.extension,
            "compression": self.compression,
            "parallel": self.parallel,
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
    comp = _COMPRESSION.search(opts)
    cols = [c.strip() for c in m.group("cols").split(",")] if m.group("cols") else None
    fmt = _detect_format(opts)
    json_arg = None
    if fmt == "JSON":
        jarg = _JSON_ARG.search(opts)
        json_arg = jarg.group(1) if jarg else "auto"  # Redshift defaults to 'auto'
    return CopyCommand(
        table=m.group("table"),
        columns=cols,
        uri=m.group("uri"),
        fmt=fmt,
        delimiter=_unescape_delim(delim.group(1)) if delim else None,
        null_as=nullas.group(1) if nullas else None,
        quote=quote.group(1) if quote else None,
        ignore_header=int(ihdr.group(1)) if ihdr else 0,
        json_arg=json_arg,
        compression=comp.group(1).lower() if comp else None,
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
        allowoverwrite=bool(_ALLOWOVERWRITE.search(opts)),
        cleanpath=bool(_CLEANPATH.search(opts)),
        partition_by=[c.strip().strip('"') for c in pb.group(1).split(",") if c.strip()]
        if (pb := _PARTITION_BY.search(opts))
        else [],
        include=bool(pb and pb.group(2)),
        manifest=bool(mf := _MANIFEST.search(opts)),
        verbose=bool(mf and mf.group(1)),
        extension=ex.group(1) if (ex := _EXTENSION.search(opts)) else None,
        compression=cm.group(1).lower() if (cm := _COMPRESSION.search(opts)) else None,
        parallel=not _PARALLEL_OFF.search(opts),
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
    """Return the field delimiter: the given one, else ',' for CSV and '|' for TEXT."""
    if opts.get("delimiter"):
        return opts["delimiter"]
    return "," if fmt == "CSV" else "|"


def _s3_object_keys(s3, bucket: str, prefix: str) -> list[str]:
    """Every object key under an s3 prefix (a single key matches itself)."""
    return [
        o["Key"]
        for o in s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    ]


_COMPRESSION_EXT = {"gzip": ".gz", "bzip2": ".bz2", "zstd": ".zst"}
_HIVE_DEFAULT = "__HIVE_DEFAULT_PARTITION__"


def _unload_extension(fmt: str, options: dict) -> str:
    """Return the file extension Redshift gives an unloaded file.

    EXTENSION when given; else ``.parquet`` for Parquet; else the compression's
    (``.gz``/``.bz2``/``.zst``); else none (Redshift adds no ``.csv``).
    """
    if options.get("extension"):
        return "." + options["extension"].lstrip(".")
    if fmt == "PARQUET":
        return ".parquet"
    return _COMPRESSION_EXT.get(options.get("compression") or "", "")


def _partition_value(value) -> str:
    """Return a partition folder's value, Hive style (NULL -> __HIVE_DEFAULT_PARTITION__)."""
    if value is None:
        return _HIVE_DEFAULT
    if isinstance(value, bool):
        return "true" if value else "false"
    return urllib.parse.quote(str(value), safe="-_.:~ ")


def _render(fmt: str, options: dict, names, rows, oids) -> bytes:
    """Serialize rows (dicts) as Parquet, CSV/text or JSON lines."""
    if fmt == "PARQUET":
        import pyarrow as pa
        import pyarrow.parquet as pq

        columns = {n: [_from_pg(r[n], oids[n]) for r in rows] for n in names}
        table = pa.table(columns) if names else pa.table({})
        buf = io.BytesIO()
        pq.write_table(
            table, buf, coerce_timestamps="us", allow_truncated_timestamps=True
        )
        return buf.getvalue()
    if fmt == "JSON":
        # NOTE: Redshift's docs say booleans are unloaded "as t or f"; JSON
        # true/false here until checked on Redshift
        def cell(value, oid):
            """Return a cell's value as JSON can hold it."""
            value = _from_pg(value, oid)
            if isinstance(value, _dt.date | _dt.time):
                return (
                    value.isoformat(sep=" ")
                    if isinstance(value, _dt.datetime)
                    else value.isoformat()
                )
            if isinstance(value, decimal.Decimal):
                return float(value)
            return value

        lines = [
            json.dumps({n: cell(r[n], oids[n]) for n in names}, ensure_ascii=False)
            for r in rows
        ]
        body = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
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
        for r in rows:
            writer.writerow(
                [null_as if r[c] is None else _csv_cell(r[c]) for c in names]
            )
        body = sio.getvalue().encode("utf-8")
    return _compress(body, options.get("compression"))


def _compress(body: bytes, compression: str | None) -> bytes:
    """Compress an unloaded text file as GZIP / BZIP2 / ZSTD asks."""
    if compression == "gzip":
        import gzip

        return gzip.compress(body)
    if compression == "bzip2":
        import bz2

        return bz2.compress(body)
    if compression == "zstd":
        import zstandard

        return zstandard.ZstdCompressor().compress(body)
    return body


def do_unload(
    plpy, query: str, uri: str, fmt: str = "PARQUET", opts: str = "{}"
) -> int:
    """Run ``query`` and write its result to ``uri``. Returns the row count.

    Files are named as Redshift names them: ``<prefix>0000_part_00`` (``000`` with
    PARALLEL OFF), plus ``.parquet`` for Parquet, the compression's extension, or
    EXTENSION. PARTITION BY writes ``col=value/`` folders (without the partition
    columns, unless INCLUDE); MANIFEST writes ``<prefix>manifest``.

    Runs via SPI on the caller's session, so temp tables created earlier in the
    same batch are visible. Called by the ``oblako_unload_to_s3`` plpython3u UDF.
    """
    fmt = fmt.upper()
    if fmt not in _SUPPORTED_FORMATS:
        plpy.error(f"unsupported UNLOAD format {fmt}; use PARQUET, CSV, JSON or text")
    options = json.loads(opts) if opts else {}
    if options.get("cleanpath") and options.get("allowoverwrite"):
        plpy.error(
            "You can't specify the CLEANPATH option with the ALLOWOVERWRITE option."
        )
    if fmt == "PARQUET" and options.get("compression"):
        plpy.error("PARQUET can't be used with GZIP, BZIP2 or ZSTD")
    bucket, prefix = _bucket_key(uri)
    partition_by = options.get("partition_by") or []
    if partition_by and prefix and not prefix.endswith("/"):
        prefix += "/"  # as Redshift adds it with PARTITION BY
    res = plpy.execute(query)
    names = list(res.colnames())
    oids = dict(zip(names, res.coltypes()))
    rows = list(res)
    missing = [c for c in partition_by if c not in names]
    if missing:
        plpy.error(f"PARTITION BY column {missing[0]} is not in the query's results")
    stem = "000" if not options.get("parallel", True) else "0000_part_00"
    ext = _unload_extension(fmt, options)
    files: dict[str, list] = {}
    for r in rows:
        folder = "".join(f"{c}={_partition_value(r[c])}/" for c in partition_by)
        files.setdefault(folder, []).append(r)
    if not files and not partition_by and fmt != "JSON":
        files[""] = []  # Redshift may write an empty file for zero rows
    written = [f"{prefix}{folder}{stem}{ext}" for folder in files]
    manifest_key = f"{prefix}manifest"
    # Redshift refuses to overwrite unless ALLOWOVERWRITE; CLEANPATH clears first,
    # with PARTITION BY only the folders that receive files
    s3 = s3_client()
    scopes = [f"{prefix}{folder}" for folder in files] if partition_by else [prefix]
    existing = sorted(
        {k for scope in scopes for k in _s3_object_keys(s3, bucket, scope)}
    )
    if options.get("manifest"):
        existing += [
            k for k in _s3_object_keys(s3, bucket, manifest_key) if k == manifest_key
        ]
    if existing and options.get("cleanpath"):
        for k in existing:
            s3.delete_object(Bucket=bucket, Key=k)
    elif existing and not options.get("allowoverwrite"):
        plpy.error(
            "Specified unload destination on S3 is not empty. Consider using a "
            "different bucket / prefix, manually removing the target files in S3, "
            "or using the ALLOWOVERWRITE option."
        )
    keep = [n for n in names if n not in partition_by or options.get("include")]
    entries: list[dict] = []
    sizes: list[int] = []
    for (folder, group), key in zip(files.items(), written):
        body = _render(fmt, options, keep, group, oids)
        s3.put_object(Bucket=bucket, Key=key, Body=body)
        meta: dict[str, int] = {"content_length": len(body)}
        if options.get("verbose"):
            meta["record_count"] = len(group)
        entries.append({"url": f"s3://{bucket}/{key}", "meta": meta})
        sizes.append(len(body))
    if options.get("manifest"):
        manifest: dict = {"entries": entries}
        if options.get("verbose"):
            manifest["schema"] = {
                "elements": [
                    {"name": n, "type": {"base": _pg_type_name(plpy, oids[n])}}
                    for n in keep
                ]
            }
            manifest["meta"] = {
                "content_length": sum(sizes),
                "record_count": len(rows),
            }
            manifest["author"] = {"name": "Amazon Redshift", "version": "1.0.0"}
        s3.put_object(
            Bucket=bucket, Key=manifest_key, Body=json.dumps(manifest).encode("utf-8")
        )
    return len(rows)


def _pg_type_name(plpy, oid: int) -> str:
    """Return the type name for a manifest's schema (``integer``, ``character varying``)."""
    return plpy.execute(f"SELECT format_type({int(oid)}, NULL) AS t")[0]["t"]


def _csv_cell(value) -> str:
    """Render a plpython value for a delimited cell (bool -> true/false)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _target_columns(plpy, table: str) -> list[str]:
    """Return the table's columns in definition order (for positional CSV/TEXT COPY)."""
    q = plpy.execute(
        "SELECT attname FROM pg_attribute "
        f"WHERE attrelid = {plpy.quote_literal(table)}::regclass "
        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
    )
    return [r["attname"] for r in q]


def _target_column_types(plpy, table: str, columns: list[str]) -> list[str]:
    """Return the PostgreSQL type of each target column, for a typed prepared INSERT."""
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


# --- JSON COPY (FORMAT JSON 'auto' | 'auto ignorecase' | 'noshred' | jsonpaths) ---
def _decompress(data: bytes, compression: str | None) -> bytes:
    """Decompress a COPY body.

    Honors the COPY token, and also auto-detects a gzip magic header so a
    Firehose-written ``.gz`` object loads without the keyword.
    """
    comp = (compression or "").lower()
    if comp == "gzip" or (not comp and data[:2] == b"\x1f\x8b"):
        import gzip

        return gzip.decompress(data)
    if comp == "bzip2":
        import bz2

        return bz2.decompress(data)
    if comp == "zstd":
        import zstandard

        return zstandard.ZstdDecompressor().decompress(data)
    return data


def _iter_json_records(data: bytes):
    """Yield JSON objects from a COPY JSON body.

    The body is a top-level array of objects, or concatenated / newline-separated
    objects (Redshift and Firehose both occur).
    """
    text = data.decode("utf-8").strip()
    if not text:
        return
    if text[0] == "[":
        yield from json.loads(text)
        return
    dec = json.JSONDecoder()
    idx, n = 0, len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        obj, idx = dec.raw_decode(text, idx)
        yield obj


_JSONPATH_TOKEN = re.compile(r"\.([A-Za-z_$][\w$]*)|\['([^']*)'\]|\[(\d+)\]")


def _json_path(obj, path: str):
    """Evaluate a Redshift jsonpaths expression on a record.

    Supports ``$`` (whole record), ``$.a.b``, ``$['a']['b']`` and ``$[0]``.
    """
    if path.strip() in ("$", "$."):
        return obj
    cur = obj
    for dot, bracket, index in _JSONPATH_TOKEN.findall(path):
        if cur is None:
            return None
        if index:
            i = int(index)
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
        else:
            cur = cur.get(dot or bracket) if isinstance(cur, dict) else None
    return cur


def _load_jsonpaths(s3, uri: str) -> list[str]:
    """Fetch and parse a jsonpaths file (``{"jsonpaths": [ ... ]}``) from s3."""
    bucket, k = _bucket_key(uri)
    doc = json.loads(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
    paths = doc.get("jsonpaths")
    if not isinstance(paths, list):
        raise ValueError("jsonpaths file must contain a 'jsonpaths' array")
    return paths


def _json_rows(plpy, s3, bucket, keys, target_cols, json_arg, compression):
    """Shred JSON object(s) under a prefix into rows aligned to ``target_cols``.

    Mirrors Redshift's FORMAT JSON options: ``auto`` (match keys to column names),
    ``auto ignorecase``, ``noshred`` (whole doc into one SUPER column), or a
    jsonpaths s3 file (paths mapped positionally to the target columns). Nested
    objects/arrays land in SUPER/jsonb columns via ``_insert_rows``.
    """
    mode = (json_arg or "auto").strip()
    lower = mode.lower()
    paths = None
    if lower not in ("auto", "auto ignorecase", "noshred"):
        paths = _load_jsonpaths(s3, mode)  # the arg is a jsonpaths s3:// uri
        if len(paths) != len(target_cols):
            plpy.error(
                f"jsonpaths has {len(paths)} paths but the target has "
                f"{len(target_cols)} column(s)"
            )
    if lower == "noshred" and len(target_cols) != 1:
        plpy.error("FORMAT JSON 'noshred' requires a single (SUPER) target column")
    rows = []
    for k in keys:
        data = _decompress(
            s3.get_object(Bucket=bucket, Key=k)["Body"].read(), compression
        )
        for rec in _iter_json_records(data):
            if lower == "noshred":
                rows.append([rec])
            elif paths is not None:
                rows.append([_json_path(rec, p) for p in paths])
            elif lower == "auto ignorecase":
                low = (
                    {kk.lower(): vv for kk, vv in rec.items()}
                    if isinstance(rec, dict)
                    else {}
                )
                rows.append([low.get(c.lower()) for c in target_cols])
            else:  # auto
                rows.append(
                    [rec.get(c) if isinstance(rec, dict) else None for c in target_cols]
                )
    return rows


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

    if fmt == "JSON":
        # Shred JSON per the FORMAT JSON option; nested values go to SUPER columns.
        target_cols = list(columns) if columns else _target_columns(plpy, table)
        rows = _json_rows(
            plpy,
            s3,
            bucket,
            keys,
            target_cols,
            options.get("json_arg"),
            options.get("compression"),
        )
        return _insert_rows(plpy, table, target_cols, rows)

    # CSV / default TEXT: positional load. An empty field or the NULL sentinel
    # becomes NULL (Redshift's default for a delimited empty field).
    delim = _delimiter_for(fmt, options)
    null_as = options.get("null_as")
    ignore = int(options.get("ignore_header") or 0)
    quote = options.get("quote") or '"'
    target_cols = list(columns) if columns else _target_columns(plpy, table)
    rows: list[list] = []
    for k in keys:
        text = _decompress(
            s3.get_object(Bucket=bucket, Key=k)["Body"].read(),
            options.get("compression"),
        ).decode("utf-8")
        reader = csv.reader(io.StringIO(text), delimiter=delim, quotechar=quote)
        file_rows = list(reader)[ignore:]
        for raw in file_rows:
            rows.append([None if (v == "" or v == null_as) else v for v in raw])
    return _insert_rows(plpy, table, target_cols, rows)
