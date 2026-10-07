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
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _array(names: list[str]) -> str:
    return "ARRAY[" + ", ".join(_literal(n) for n in names) + "]::text[]"


def _call(lead: str, function: str, args: list[str], tail: str) -> str:
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
