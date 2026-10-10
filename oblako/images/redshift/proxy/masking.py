"""Redshift dynamic data masking statements, on redshift-local.

PostgreSQL has no masking policies, so the proxy turns Redshift's statements into
calls to pg_oblako functions (initdb.d/15_masking.sql) that keep policies and
their attachments in tables and answer svv_masking_policy and
svv_attached_masking_policy as Redshift does:

    CREATE MASKING POLICY [IF NOT EXISTS] name WITH (inputs) USING (expression)
    ALTER MASKING POLICY name USING (expression)
    DROP MASKING POLICY name
    ATTACH MASKING POLICY name ON relation (outputs) [USING (inputs)]
        TO { user | ROLE role | PUBLIC } [PRIORITY n]
    DETACH MASKING POLICY name ON relation (outputs)
        FROM { user | ROLE role | PUBLIC }

Each becomes one DO statement, so it also runs on the extended protocol. A form
Redshift refuses (TO GROUP, say) is left as written and fails in PostgreSQL's
parser, as it fails in Redshift's. A ``database.`` prefix on a policy is
accepted and dropped: redshift-local has one database per connection.
"""

from __future__ import annotations

import re

_START = re.compile(
    r"(?is)^(\s*)(create|alter|drop|attach|detach)\s+masking\s+policy\s+"
)
_IDENT = r'(?:"(?:[^"]|"")+"|[a-z_][\w$]*)'
_NAME = re.compile(rf"(?is)^(?:{_IDENT}\.)?({_IDENT})\s*")
_RELATION = re.compile(rf"(?is)^({_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}})\s*")
_GRANTEE = re.compile(rf"(?is)^(?:(role)\s+({_IDENT})|(public)\b|({_IDENT}))\s*")


def _ident(raw: str) -> str:
    """Return the name an identifier denotes: quoted verbatim, bare folded."""
    if raw.startswith('"'):
        return raw[1:-1].replace('""', '"')
    return raw.lower()


def _literal(value: str) -> str:
    """Return ``value`` as a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _parens(text: str, pos: int) -> tuple[str, int]:
    """Return what the parentheses at ``pos`` enclose and where they end."""
    if pos >= len(text) or text[pos] != "(":
        raise ValueError("expected (")
    depth, i, quote = 0, pos, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[pos + 1 : i], i + 1
        i += 1
    raise ValueError("unbalanced parentheses")


def _split(text: str) -> list[str]:
    """Split on the commas outside parentheses and quotes."""
    parts, depth, start, quote = [], 0, 0, None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return [p for p in parts if p]


def _skip(text: str, pos: int) -> int:
    """Return the first position at or after ``pos`` that isn't whitespace."""
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _array(names: list[str]) -> str:
    """Return a text[] literal of ``names``."""
    return "ARRAY[" + ", ".join(_literal(n) for n in names) + "]::text[]"


def _call(lead: str, function: str, args: list[str], tail: str) -> str:
    """Return a DO statement that calls one pg_oblako masking function."""
    return (
        f"{lead}DO $oblako_ddm$ BEGIN PERFORM pg_oblako.{function}("
        + ", ".join(args)
        + f"); END $oblako_ddm${tail}"
    )


def rewrite_masking(stmt: str) -> str:
    """Rewrite one masking statement into a pg_oblako call; others are returned as is."""
    m = _START.match(stmt)
    if not m:
        return stmt
    lead, verb = m.group(1), m.group(2).lower()
    body = stmt[m.end() :]
    semi = ";" if body.rstrip().endswith(";") else ""
    body = body.rstrip().rstrip(";").rstrip()
    try:
        return _rewrite(verb, lead, body, semi) or stmt
    except ValueError:
        return stmt  # malformed: PostgreSQL's parser reports it


def _rewrite(verb: str, lead: str, body: str, semi: str) -> str | None:
    """Rewrite one masking statement into its pg_oblako call, or return None."""
    if_not_exists = False
    if verb == "create" and (m := re.match(r"(?is)^if\s+not\s+exists\s+", body)):
        if_not_exists, body = True, body[m.end() :]
    name_m = _NAME.match(body)
    if not name_m:
        return None
    policy = _literal(_ident(name_m.group(1)))
    rest = body[name_m.end() :]

    if verb == "drop":
        return _call(lead, "ddm_drop", [policy], semi) if not rest.strip() else None

    if verb == "create":
        m = re.match(r"(?is)^with\s*", rest)
        if not m:
            return None
        inputs, end = _parens(rest, m.end())
        names, types = [], []
        for item in _split(inputs):
            col = re.match(rf"(?is)^({_IDENT})\s+(.+)$", item)
            if not col:
                return None
            names.append(_ident(col.group(1)))
            types.append(col.group(2).strip())
        rest = rest[_skip(rest, end) :]
        m = re.match(r"(?is)^using\s*", rest)
        if not m:
            return None
        expr, end = _parens(rest, m.end())
        if rest[end:].strip():
            return None
        return _call(
            lead,
            "ddm_create",
            [
                policy,
                _array(names),
                _array(types),
                _literal(expr.strip()),
                "true" if if_not_exists else "false",
            ],
            semi,
        )

    if verb == "alter":
        m = re.match(r"(?is)^using\s*", rest)
        if not m:
            return None
        expr, end = _parens(rest, m.end())
        if rest[end:].strip():
            return None
        return _call(lead, "ddm_alter", [policy, _literal(expr.strip())], semi)

    # ATTACH / DETACH: ON relation (outputs) ...
    m = re.match(r"(?is)^on\s+", rest)
    if not m:
        return None
    rest = rest[m.end() :]
    rel = _RELATION.match(rest)
    if not rel:
        return None
    relation = _literal(rel.group(1))
    rest = rest[rel.end() :]
    outputs, end = _parens(rest, 0)
    out_names = [_ident(o) for o in _split(outputs)]
    rest = rest[_skip(rest, end) :]
    in_names = out_names
    if verb == "attach":
        m = re.match(r"(?is)^using\s*", rest)
        if m:
            inputs, end = _parens(rest, m.end())
            in_names = [_ident(i) for i in _split(inputs)]
            rest = rest[_skip(rest, end) :]
    keyword = "to" if verb == "attach" else "from"
    m = re.match(rf"(?is)^{keyword}\s+", rest)
    if not m:
        return None
    rest = rest[m.end() :]
    g = _GRANTEE.match(rest)
    if not g:
        return None
    if g.group(1):
        grantee, grantee_type = _ident(g.group(2)), "role"
    elif g.group(3):
        grantee, grantee_type = "public", "public"
    else:
        grantee, grantee_type = _ident(g.group(4)), "user"
    rest = rest[g.end() :]
    if verb == "detach":
        if rest.strip():
            return None
        return _call(
            lead,
            "ddm_detach",
            [
                policy,
                relation,
                _array(out_names),
                _literal(grantee),
                _literal(grantee_type),
            ],
            semi,
        )
    priority = "0"
    m = re.match(r"(?is)^priority\s+(-?\d+)\s*$", rest)
    if m:
        priority = m.group(1)
    elif rest.strip():
        return None
    return _call(
        lead,
        "ddm_attach",
        [
            policy,
            relation,
            _array(out_names),
            _array(in_names),
            _literal(grantee),
            _literal(grantee_type),
            priority,
        ],
        semi,
    )


# ---------------------------------------------------------------------------
# Query time: masked table reads
# ---------------------------------------------------------------------------
# words that end a FROM list, or can't be a table's alias
_CLAUSE_END = {
    "where",
    "group",
    "order",
    "limit",
    "offset",
    "union",
    "except",
    "intersect",
    "having",
    "window",
    "fetch",
    "for",
    "returning",
    "set",
    "values",
    "into",
    "select",
    "with",
}
_NOT_ALIAS = _CLAUSE_END | {
    "join",
    "inner",
    "left",
    "right",
    "full",
    "outer",
    "cross",
    "natural",
    "on",
    "using",
    "tablesample",
}
_WORD = re.compile(r"[a-z_][\w$]*", re.I)
_QUOTED = re.compile(r'"(?:[^"]|"")*"')


def _skip_noise(sql: str, i: int) -> int:
    """Return the index past whitespace and comments from ``i``."""
    while i < len(sql):
        if sql[i].isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = len(sql) if end < 0 else end + 2
        else:
            break
    return i


def _ident_at(sql: str, i: int) -> tuple[str, str, int] | None:
    """Return (name as written, name it denotes, end) for an identifier at ``i``."""
    m = _QUOTED.match(sql, i)
    if m:
        return m.group(0), m.group(0)[1:-1].replace('""', '"'), m.end()
    m = _WORD.match(sql, i)
    if m:
        return m.group(0), m.group(0).lower(), m.end()
    return None


def _relation_at(sql: str, i: int):
    """Return ((schema, table), end) for a table name at ``i``, or None."""
    first = _ident_at(sql, i)
    if first is None:
        return None
    end = first[2]
    j = _skip_noise(sql, end)
    if j < len(sql) and sql[j] == ".":
        second = _ident_at(sql, _skip_noise(sql, j + 1))
        if second is None:
            return None
        k = _skip_noise(sql, second[2])
        if k < len(sql) and sql[k] in ".(":  # db.schema.table, or a function
            return None
        return (first[1], second[1]), second[2]
    if j < len(sql) and sql[j] == "(":
        return None  # a set-returning function
    return ("public", first[1]), end


def rewrite_reads(sql: str, masked: dict[tuple[str, str], str]) -> str:
    """Replace each masked table read in a query with its masking SELECT.

    ``masked`` maps (schema, table) to the SELECT that stands in for the table
    (from pg_oblako.ddm_masked_tables). A read is a table in a FROM list or after
    JOIN; the target of DELETE FROM is not one. An unqualified name is taken as
    public's, as Redshift's default search path has it. The replacement keeps the
    table's name as its alias, so table.column still resolves.
    """
    out: list[str] = []
    i, last = 0, 0
    depth = 0
    from_depth: list[int] = []  # paren depths at which a FROM list is open
    previous = ""  # the last keyword seen
    expect_relation = False
    only_at: int | None = None  # where a FROM ONLY began
    while i < len(sql):
        ch = sql[i]
        if ch == "'":  # a string: skip to its end
            end = i + 1
            while end < len(sql):
                if sql[end] == "'" and sql[end + 1 : end + 2] == "'":
                    end += 2
                elif sql[end] == "'":
                    break
                else:
                    end += 1
            i = end + 1
            continue
        if ch == "$":
            m = re.match(r"\$[a-z_]*\$", sql[i:], re.I)
            if m:
                end = sql.find(m.group(0), i + len(m.group(0)))
                i = len(sql) if end < 0 else end + len(m.group(0))
                continue
        if sql.startswith("--", i) or sql.startswith("/*", i):
            i = _skip_noise(sql, i)
            continue
        if expect_relation and (ch.isalpha() or ch in '_"'):
            word = _WORD.match(sql, i)
            if word and word.group(0).lower() == "only":  # FROM ONLY t: still t
                only_at = i
                i = _skip_noise(sql, word.end())
                continue
            expect_relation = False
            found = _relation_at(sql, i)
            if found and found[0] in masked:
                (schema, table), end = found
                j = _skip_noise(sql, end)
                alias = _ident_at(sql, j)
                has_alias = alias is not None and alias[1] not in _NOT_ALIAS
                out.append(sql[last : only_at if only_at is not None else i])
                out.append(f"({masked[(schema, table)]})")
                if not has_alias:
                    out.append(' AS "' + table.replace('"', '""') + '"')
                last = i = end
                only_at = None
                continue
            only_at = None
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            while from_depth and from_depth[-1] > depth:
                from_depth.pop()
        elif ch == ";":
            from_depth.clear()
            previous = ""
        elif ch == "," and from_depth and from_depth[-1] == depth:
            expect_relation = True
        word = _WORD.match(sql, i) if (ch.isalpha() or ch == "_") else None
        if word and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] in '_$."')):
            w = word.group(0).lower()
            if w == "from":
                if previous != "delete":
                    from_depth.append(depth)
                    expect_relation = True
            elif w == "join":
                expect_relation = True
            elif w in _CLAUSE_END and from_depth and from_depth[-1] == depth:
                from_depth.pop()
            elif w != "lateral":  # LATERAL keeps the relation coming
                expect_relation = False
            previous = w
            i = word.end()
            continue
        if not ch.isspace() and ch not in "(,":
            expect_relation = False  # an expression, not a table name
        i += 1
    out.append(sql[last:])
    return "".join(out)
