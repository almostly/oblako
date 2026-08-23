"""Rewrite Redshift ``LISTAGG`` into PostgreSQL ``string_agg``, for the wire proxy.

Redshift:   LISTAGG([DISTINCT] expr [, delim]) [WITHIN GROUP (ORDER BY ...)]
PostgreSQL: string_agg([DISTINCT] (expr)::text, delim [ORDER BY ...])

``string_agg`` is native (C) and carries the ordering inside its argument list, so
the only work is moving Redshift's ``WITHIN GROUP (ORDER BY ...)`` into it and
defaulting the delimiter to '' when omitted. Because the delimiter is a string
literal, the whole call has to be scanned with balanced-paren / quote awareness
rather than split on strings first.
"""

from __future__ import annotations

import re

_WITHIN = re.compile(r"(?i)within\s+group\s*\(")
_DISTINCT = re.compile(r"(?i)distinct\b")


def _skip_string(s: str, i: int) -> int:
    """Index just past the single-quoted literal starting at ``s[i]`` ('')."""
    n = len(s)
    i += 1
    while i < n:
        if s[i] == "'":
            if i + 1 < n and s[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _balanced(s: str, i: int) -> tuple[str, int]:
    """From ``s[i]=='('`` return (inner-without-parens, index past the matching ')')."""
    n = len(s)
    depth = 0
    start = i
    while i < n:
        c = s[i]
        if c == "'":
            i = _skip_string(s, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[start + 1 : i], i + 1
        i += 1
    return s[start + 1 :], n


def _split_top_commas(s: str) -> list[str]:
    """Split on commas that are not inside parentheses or string literals."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "'":
            j = _skip_string(s, i)
            buf.append(s[i:j])
            i = j
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _build_string_agg(args: str, order: str) -> str:
    """Build the ``string_agg(...)`` equivalent of a LISTAGG call's args."""
    a = args.strip()
    distinct = ""
    if _DISTINCT.match(a):
        distinct = "DISTINCT "
        a = a[len("distinct") :].lstrip()
    parts = _split_top_commas(a)
    expr = parts[0].strip()
    delim = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "''"
    order_clause = f" {order}" if order else ""
    return f"string_agg({distinct}({expr})::text, {delim}{order_clause})"


def rewrite_listagg(sql: str) -> str:
    """Rewrite every ``LISTAGG(...) [WITHIN GROUP (...)]`` to ``string_agg(...)``."""
    low = sql.lower()
    if "listagg" not in low:
        return sql
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":  # copy string literals verbatim
            j = _skip_string(sql, i)
            out.append(sql[i:j])
            i = j
            continue
        prev = sql[i - 1] if i > 0 else ""
        if low.startswith("listagg", i) and not (prev.isalnum() or prev == "_"):
            k = i + len("listagg")
            while k < n and sql[k].isspace():
                k += 1
            if k < n and sql[k] == "(":
                args, after = _balanced(sql, k)
                order = ""
                p = after
                while p < n and sql[p].isspace():
                    p += 1
                m = _WITHIN.match(sql, p)
                if m:
                    wg, after = _balanced(sql, m.end() - 1)
                    order = wg.strip()
                out.append(_build_string_agg(args, order))
                i = after
                continue
        out.append(c)
        i += 1
    return "".join(out)
