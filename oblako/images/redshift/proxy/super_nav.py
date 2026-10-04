"""Redshift SUPER (PartiQL) dot-navigation -> a jsonb path, for the wire proxy.

Redshift lets you walk a SUPER value with PartiQL: ``data.a.b`` (dot) and
``data['a'][0]`` (bracket). The bracket form already works on our SUPER type (a
domain over jsonb; PG14+ subscripts jsonb natively), but the *dot* form can't:
PostgreSQL parses ``a.b.c`` as ``table.column`` / ``schema.table.column``. So the
proxy rewrites a dot/bracket chain rooted at a known SUPER column into a jsonb
path extraction:

    c.data.customer.name      ->  (c.data #>> ARRAY['customer','name'])
    data.items[0].sku         ->  (data #>> ARRAY['items','0','sku'])

The leaf is text (``#>>``), so string/equality filters work
(``WHERE data.type = 'premium'``). A chain that is a whole item of a SELECT list
(``SELECT data.customer.name, ...``) returns the SUPER value as JSON text instead,
``"Ann"`` with its quotes, which is how Redshift sends SUPER to the driver; a cast
(``data.customer.name::varchar``) gives the plain string, as on Redshift. Which columns are SUPER is learned from the
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
        return f"({m.group(1)} {_TAG} ARRAY[{elems}])"

    tagged = _map_code(sql, lambda code: pat.sub(repl, code))
    if _TAG not in tagged:
        return tagged
    return _finish(tagged)


# ---------------------------------------------------------------------------------
# Projection items: SUPER as JSON text
# ---------------------------------------------------------------------------------
# marks a rewritten chain until _finish decides its operator
_TAG = "#>>/*oblako-nav*/"
_TAGGED = re.compile(
    r"\((?:\w+\.)?\w+ " + re.escape(_TAG) + r" ARRAY\[(?:'(?:[^']|'')*'(?:, )?)+\]\)"
)
# a SELECT list ends, or never began, at one of these at the same depth
_NOT_SELECT_LIST = {
    "from",
    "where",
    "having",
    "on",
    "using",
    "set",
    "values",
    "join",
    "limit",
    "into",
    "returning",
}
# after an item, these start an operator expression rather than end the item
_OPERATOR_WORDS = {
    "is",
    "and",
    "or",
    "not",
    "like",
    "ilike",
    "in",
    "between",
    "similar",
    "collate",
    "at",
    "escape",
    "isnull",
    "notnull",
}
_ITEM_END_WORDS = {
    "asc",
    "desc",
    "nulls",
    "from",
    "as",
    "into",
    "union",
    "except",
    "intersect",
    "order",
    "limit",
    "where",
    "group",
    "having",
    "offset",
    "fetch",
}


def _scan(sql: str) -> tuple[list[bool], list[int]]:
    """Return, per character, whether it is code (not a literal or comment) and its depth."""
    code = [True] * len(sql)
    depth = [0] * len(sql)
    d, i, n = 0, 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'" or c == '"':
            j = i + 1
            while j < n and not (sql[j] == c and (j + 1 >= n or sql[j + 1] != c)):
                j += 2 if sql[j] == c else 1
            for k in range(i, min(j + 1, n)):
                code[k], depth[k] = False, d
            i = j + 1
            continue
        if sql.startswith("--", i) or sql.startswith("/*", i):
            j = sql.find("\n", i) if sql.startswith("--", i) else sql.find("*/", i) + 1
            j = n - 1 if j <= 0 else j
            for k in range(i, j + 1):
                code[k], depth[k] = False, d
            i = j + 1
            continue
        if c == "(":
            depth[i] = d
            d += 1
        elif c == ")":
            d -= 1
            depth[i] = d
        else:
            depth[i] = d
        i += 1
    return code, depth


def _prev_token(sql, code, start):
    """Return (token, index) of the last code token before ``start``, or ("", -1)."""
    i = start - 1
    while i >= 0 and (not code[i] or sql[i].isspace()):
        i -= 1
    if i < 0:
        return "", -1
    if sql[i].isalnum() or sql[i] == "_":
        j = i
        while j > 0 and code[j - 1] and (sql[j - 1].isalnum() or sql[j - 1] == "_"):
            j -= 1
        return sql[j : i + 1].lower(), j
    return sql[i], i


def _next_token(sql, code, end):
    """Return the first code token at or after ``end``, or "" at the end."""
    i = end
    while i < len(sql) and (not code[i] or sql[i].isspace()):
        i += 1
    if i >= len(sql):
        return ""
    if sql[i].isalnum() or sql[i] == "_":
        j = i
        while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
            j += 1
        return sql[i:j].lower()
    return sql[i]


def _in_select_list(sql, code, depth, start) -> bool:
    """Whether the expression at ``start`` stands alone as an item of a SELECT list.

    GROUP BY and ORDER BY lists count too, so an item grouped or ordered by is
    spelled exactly as it is selected (PostgreSQL matches them by expression).
    """
    tok, at = _prev_token(sql, code, start)
    if tok not in ("select", "distinct", "all", "by", ","):
        return False
    d = depth[start]
    while at >= 0:
        if tok in ("select", "by") and depth[at] == d:
            return True
        if tok == "(" and depth[at] < d:
            return False  # inside parentheses that aren't a subquery's own
        if tok in _NOT_SELECT_LIST and depth[at] == d:
            return False
        tok, at = _prev_token(sql, code, at)
    return False


# a cast right after a navigated item: ::varchar, ::int, ::character varying(20)
_CAST = re.compile(
    r"\s*::\s*[A-Za-z_]\w*(?:\s+(?:precision|varying))?(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?"
)


def _item_ends(nxt: str) -> bool:
    """Whether the token after an expression ends a SELECT-list item."""
    return (
        nxt in ("", ",", ")", ";")
        or nxt in _ITEM_END_WORDS
        or (nxt.isidentifier() and nxt not in _OPERATOR_WORDS)
    )


def _alias_needed(nxt: str) -> bool:
    """Whether an item ending before ``nxt`` has no alias of its own."""
    return nxt != "as" and not (
        nxt.isidentifier() and nxt not in _OPERATOR_WORDS | _ITEM_END_WORDS
    )


def _finish(sql: str) -> str:
    """Turn each tagged chain into ``#>>`` (text), or JSON text as a bare SELECT item.

    A selected item is named as Redshift names it, after the last key of its path
    (``data.tags[0]`` is ``tags``), cast or not, unless it has an alias.
    """
    code, depth = _scan(sql)
    out, pos = [], 0
    for m in _TAGGED.finditer(sql):
        if not code[m.start()]:
            continue
        keys = [
            k.replace("''", "'") for k in re.findall(r"'((?:[^']|'')*)'", m.group(0))
        ]
        names = [k for k in keys if not k.isdigit()]
        alias = ' AS "' + names[-1].replace('"', '""') + '"' if names else ""
        listed = _in_select_list(sql, code, depth, m.start())
        in_by = _prev_token(sql, code, m.start())[0] == "by"
        nxt = _next_token(sql, code, m.end())
        expr, end = m.group(0), m.end()
        if listed and _item_ends(nxt):  # the SUPER value as Redshift sends it
            expr = "(" + expr[1:-1].replace(_TAG, "#>") + ")::text"
            if not in_by and _alias_needed(nxt):
                expr += alias
        elif listed and not in_by and nxt == ":" and (cast := _CAST.match(sql, end)):
            # a cast item (data.name::varchar) keeps the path's name, as on Redshift
            after = _next_token(sql, code, cast.end())
            if _item_ends(after) and _alias_needed(after):
                expr, end = expr + cast.group(0) + alias, cast.end()
        out.append(sql[pos : m.start()] + expr)
        pos = end
    out.append(sql[pos:])
    return "".join(out).replace(_TAG, "#>>")
