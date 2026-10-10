"""Athena CTAS (``CREATE TABLE ... WITH (...) AS SELECT``) on Trino.

Athena's table properties mostly match Trino's Hive connector; the rest are
translated here. A plain CTAS lands in the Hive ``awsdatacatalog`` catalog (a
Glue table), under ``<output>/tables/<query id>`` when it names no
``external_location``, as on Athena. With ``table_type = 'ICEBERG'`` it runs in
the ``iceberg`` catalog instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CTAS = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?P<name>.+?)\s+WITH\s*\(", re.IGNORECASE | re.DOTALL
)
_HIVE_ONLY = {"external_location", "bucketed_by", "bucket_count", "partitioned_by"}
_COMPRESSION = {"SNAPPY", "GZIP", "ZSTD", "LZ4", "NONE"}


@dataclass
class Ctas:
    """A CTAS rewritten for Trino."""

    sql: str
    table: str
    iceberg: bool = False
    session: dict[str, str] = field(default_factory=dict)


def _split_top_level(text: str) -> list[str]:
    """Split on commas outside quotes, parentheses and brackets."""
    parts, depth, quote, start = [], 0, False, 0
    for i, ch in enumerate(text):
        if ch == "'":
            quote = not quote
        elif not quote and ch in "([":
            depth += 1
        elif not quote and ch in ")]":
            depth -= 1
        elif not quote and depth == 0 and ch == ",":
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _closing_paren(text: str, open_at: int) -> int:
    """Return the index of the paren closing the one at ``open_at``, skipping quotes."""
    depth, quote = 0, False
    for i in range(open_at, len(text)):
        ch = text[i]
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced WITH (...)")


def _unquote(value: str) -> str:
    """Strip surrounding single quotes from a property value, if present."""
    value = value.strip()
    return value[1:-1] if value[:1] == "'" and value[-1:] == "'" else value


def rewrite(sql: str, output_dir: str, query_id: str) -> Ctas | None:
    """Return the Trino form of an Athena CTAS, or None if ``sql`` isn't one."""
    match = _CTAS.match(sql)
    if not match:
        return None
    open_at = match.end() - 1
    close_at = _closing_paren(sql, open_at)
    rest = sql[close_at + 1 :]
    if not re.match(r"\s*AS\b", rest, re.IGNORECASE):
        return None
    props: dict[str, str] = {}
    for part in _split_top_level(sql[open_at + 1 : close_at]):
        key, _, value = part.partition("=")
        props[key.strip().lower()] = value.strip()

    iceberg = _unquote(props.pop("table_type", "'HIVE'")).upper() == "ICEBERG"
    props.pop("is_external", None)
    session: dict[str, str] = {}
    catalog = "iceberg" if iceberg else "awsdatacatalog"
    compression = props.pop("write_compression", None)
    if compression and _unquote(compression).upper() in _COMPRESSION:
        session[f"{catalog}.compression_codec"] = _unquote(compression).upper()
    if iceberg:
        for key in list(props):
            if key.startswith(("vacuum_", "optimize_")) or key in _HIVE_ONLY:
                props.pop(key)
    else:
        if "field_delimiter" in props:
            props["textfile_field_separator"] = props.pop("field_delimiter")
        props.setdefault(
            "external_location", f"'{output_dir.rstrip('/')}/tables/{query_id}'"
        )
    if "format" in props:
        props["format"] = props["format"].upper()
    with_clause = ",\n    ".join(f"{k} = {v}" for k, v in props.items())
    table = match.group("name").strip()
    return Ctas(
        sql=f"CREATE TABLE {table}\nWITH (\n    {with_clause}\n){rest}",
        table=table,
        iceberg=iceberg,
        session=session,
    )


def statement_type(sql: str) -> str:
    """Classify a statement as Athena does: DML, DDL or UTILITY."""
    first = re.match(r"[\s(]*(\w+)", sql)
    word = first.group(1).upper() if first else ""
    if word in {
        "SELECT",
        "WITH",
        "VALUES",
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "UNLOAD",
        "TABLE",
    }:
        return "DML"
    if word in {"CREATE", "DROP", "ALTER", "SHOW", "DESCRIBE", "MSCK", "USE"}:
        return "DDL"
    return "UTILITY"
