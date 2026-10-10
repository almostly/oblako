"""GRANT and REVOKE of ALTER and DROP, imported by the proxy.

Redshift grants ALTER and DROP on tables, views and schemas; PostgreSQL has no
such privileges and rejects the statement. A GRANT or REVOKE naming either
becomes one DO statement: the other privileges in it run as PostgreSQL's own
GRANT or REVOKE, and ALTER and DROP go to pg_oblako.object_privilege
(initdb.d/14_redshift_identities.sql), which keeps them for the privilege views
and for the oblako_redshift extension to enforce. Pure-stdlib.
"""

from __future__ import annotations

import re

_STATEMENT = re.compile(
    r"(?is)^(\s*)(grant|revoke)\s+(grant\s+option\s+for\s+)?(.+?)\s+on\s+(.+?)\s+"
    r"(to|from)\s+(.+?)(\s+with\s+grant\s+option)?\s*(;?)(\s*)$"
)
_SCHEMA = re.compile(r"(?is)^schema\s+(.+)$")
_ALL_TABLES = re.compile(r"(?is)^all\s+tables\s+in\s+schema\s+(.+)$")
_TABLE = re.compile(r"(?is)^(?:table\s+)?(.+)$")
_EXTRA = {"alter", "drop"}


def _split(text: str) -> list[str]:
    """Split ``text`` on commas outside parentheses and double quotes."""
    parts, depth, quoted, start = [], 0, False, 0
    for i, ch in enumerate(text):
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch in "()":
            depth += 1 if ch == "(" else -1
        elif not quoted and depth == 0 and ch == ",":
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


def _literal(value: str | None) -> str:
    """Return ``value`` as a SQL string literal (NULL for None)."""
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def _array(values: list[str | None]) -> str:
    """Return a text[] literal of ``values``."""
    return "ARRAY[" + ", ".join(_literal(v) for v in values) + "]::text[]"


def _grantee(text: str) -> tuple[str | None, str]:
    """Return a grantee's role name (None for PUBLIC) and its PostgreSQL spelling.

    PostgreSQL takes GROUP g as Redshift writes it, but not ROLE r: just r.
    """
    words = text.split(None, 1)
    keyword = words[0].lower() if len(words) == 2 else ""
    name = words[1] if keyword in ("role", "group") else text
    native = name if keyword == "role" else text
    if name.startswith('"'):
        return name[1:-1].replace('""', '"'), native
    return (None if name.lower() == "public" else name.lower()), native


def rewrite_object_privileges(stmt: str) -> str:
    """Rewrite one GRANT or REVOKE naming ALTER or DROP; leave anything else as is."""
    if not (m := _STATEMENT.match(stmt)):
        return stmt
    (
        lead,
        verb,
        option_for,
        privs,
        target,
        to_from,
        grantees,
        with_option,
        semi,
        tail,
    ) = m.groups()
    privileges = _split(privs)
    extra = [p.upper() for p in privileges if p.lower() in _EXTRA]
    if not extra:
        return stmt
    if t := _ALL_TABLES.match(target):
        kind, objects = "schema_tables", _split(t.group(1))
    elif t := _SCHEMA.match(target):
        kind, objects = "schema", _split(t.group(1))
    elif (t := _TABLE.match(target)) and not re.match(
        r"(?i)(?:database|function|procedure|language|datashare|model)\b", target
    ):
        kind, objects = "relation", _split(t.group(1))
    else:
        return stmt
    names = [_grantee(g) for g in _split(grantees)]
    grant_option = bool(with_option or option_for)
    body = []
    native = [p for p in privileges if p.lower() not in _EXTRA]
    if native:
        body.append(
            f"{verb} {option_for or ''}{', '.join(native)} ON {target} {to_from} "
            f"{', '.join(n[1] for n in names)}{with_option or ''};"
        )
    body.append(
        f"PERFORM pg_oblako.object_privilege({verb.lower() == 'grant'}, "
        f"{_array(list(extra))}, {_literal(kind)}, {_array(objects)}, "
        f"{_array([n[0] for n in names])}, {grant_option});"
    )
    return (
        f"{lead}DO $oblako_priv$ BEGIN {' '.join(body)} END $oblako_priv${semi}{tail}"
    )
