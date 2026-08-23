"""Redshift SUPER (PartiQL) dot-navigation -> a jsonb path, for the wire proxy.

Redshift lets you walk a SUPER value with PartiQL: ``data.a.b`` (dot) and
``data['a'][0]`` (bracket). The bracket form already works on our SUPER type (a
domain over jsonb; PG14+ subscripts jsonb natively), but the *dot* form can't:
PostgreSQL parses ``a.b.c`` as ``table.column`` / ``schema.table.column``. So the
proxy rewrites a dot/bracket chain rooted at a known SUPER column into a jsonb
path extraction:

    c.data.customer.name      ->  (c.data #>> ARRAY['customer','name'])
    data.items[0].sku         ->  (data #>> ARRAY['items','0','sku'])

The leaf is text (``#>>``), so projections and string/equality filters work
(``WHERE data.type = 'premium'``). Which columns are SUPER is learned from the
``CREATE TABLE`` / ``ALTER TABLE`` DDL the proxy sees (every client connects
through the proxy, so every declaration passes by). Rewrites are applied only
outside string literals, so data is never touched.

Known limits (documented, best-effort - real Redshift resolves this in the
planner with full schema scope): a numeric comparison needs an explicit cast
(``(data.age)::int > 30``); a column name that is SUPER in one table shadows a
same-named non-SUPER column elsewhere; navigation yields text, not a nested SUPER.
"""

from __future__ import annotations

import re

# Column names declared SUPER, learned from the DDL the proxy relays. Process-wide
# (single container); every CREATE/ALTER TABLE goes through the proxy.
SUPER_COLUMNS: set[str] = set()

_CREATE_OR_ALTER = re.compile(
    r"(?i)\b(?:create\s+(?:or\s+replace\s+)?(?:(?:global|local)\s+)?"
    r"(?:temp(?:orary)?\s+|unlogged\s+)?(?:materialized\s+)?"
    r"(?:table|view)|alter\s+table)\b"
)
_SUPER_COL_DEF = re.compile(r'(?i)"?(\w+)"?\s+super\b')
# CTAS / view: a `...::super AS alias` output column is SUPER too.
_SUPER_CAST_ALIAS = re.compile(r'(?i)::\s*super\s+as\s+"?(\w+)"?')
_STEP = re.compile(r"""\.(\w+)|\[(\d+)\]|\['([^']*)'\]|\["([^"]*)"\]""")


def record_super_columns(sql: str) -> None:
    """Learn SUPER column names from a CREATE/ALTER TABLE (or view / CTAS)."""
    if not _CREATE_OR_ALTER.search(sql):
        return
    for m in _SUPER_COL_DEF.finditer(sql):
        SUPER_COLUMNS.add(m.group(1).lower())
    for m in _SUPER_CAST_ALIAS.finditer(sql):
        SUPER_COLUMNS.add(m.group(1).lower())


def _chain_path(chain: str) -> list[str] | None:
    """Parse a ``.k``/``[i]``/``['k']`` chain into path parts, or None if unclean."""
    path: list[str] = []
    pos = 0
    for m in _STEP.finditer(chain):
        if m.start() != pos:
            return None
        path.append(next(g for g in m.groups() if g is not None))
        pos = m.end()
    if pos != len(chain) or not path:
        return None
    return path


def _map_code(sql: str, fn):
    """Apply ``fn`` to the parts of ``sql`` outside single-quoted string literals."""
    parts: list[tuple[bool, str]] = []
    cur: list[str] = []
    i, n, in_str = 0, len(sql), False
    while i < n:
        c = sql[i]
        if in_str:
            cur.append(c)
            if c == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                parts.append((True, "".join(cur)))
                cur = []
                in_str = False
            i += 1
        elif c == "'":
            if cur:
                parts.append((False, "".join(cur)))
            cur = ["'"]
            in_str = True
            i += 1
        else:
            cur.append(c)
            i += 1
    if cur:
        parts.append((in_str, "".join(cur)))
    return "".join(t if is_str else fn(t) for is_str, t in parts)


def rewrite_super_paths(sql: str) -> str:
    """Rewrite SUPER dot/bracket navigation into jsonb path extraction (text leaf)."""
    if not SUPER_COLUMNS or "." not in sql and "[" not in sql:
        return sql
    alt = "|".join(re.escape(c) for c in sorted(SUPER_COLUMNS, key=len, reverse=True))
    root = r"(?:\b\w+\.)?\b(?:" + alt + r")\b"
    step = r"""(?:\.\w+|\[\d+\]|\['[^']*'\]|\["[^"]*"\])"""
    pat = re.compile(r"(" + root + r")(" + step + r"+)", re.IGNORECASE)

    def repl(m: re.Match) -> str:
        chain = m.group(2)
        # Pure bracket navigation (data['a'][0]) already works via native jsonb
        # subscripting; only dot navigation needs rewriting. (Quoted keys are also
        # string literals, so _map_code never even reaches them.)
        if "." not in chain:
            return m.group(0)
        path = _chain_path(chain)
        if path is None:
            return m.group(0)
        elems = ", ".join("'" + p.replace("'", "''") + "'" for p in path)
        return f"({m.group(1)} #>> ARRAY[{elems}])"

    return _map_code(sql, lambda code: pat.sub(repl, code))
