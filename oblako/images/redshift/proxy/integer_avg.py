"""Point ``AVG(...)`` at Redshift's semantics, for the wire proxy.

Redshift's AVG of a SMALLINT, INTEGER or BIGINT returns BIGINT (the fraction is
dropped); PostgreSQL's returns NUMERIC. Which one applies depends on the
argument's type, which the proxy can't see, so the proxy doesn't decide: it
rewrites each unqualified ``avg(`` call to ``pg_oblako.avg(``, and the engine
resolves the overload. That schema holds BIGINT-returning aggregates for the
integer types and exact copies of PostgreSQL's own for every other type
(initdb.d/11_integer_avg.sql), so non-integer averages are unchanged.

The scan skips string literals, quoted identifiers and comments, leaves qualified
calls (``x.avg(``) alone, and doesn't touch ``CREATE FUNCTION/AGGREGATE avg``.
"""

from __future__ import annotations

_TARGET = "pg_oblako.avg"
_NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_$"


def _skip_quoted(s: str, i: int, quote: str) -> int:
    """Return the index just past the quoted run starting at ``s[i]`` (doubled quotes escape)."""
    n = len(s)
    i += 1
    while i < n:
        if s[i] == quote:
            if i + 1 < n and s[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _previous_word(s: str, end: int) -> str:
    """Return the identifier-like word that ends before ``end``, skipping spaces."""
    j = end
    while j > 0 and s[j - 1].isspace():
        j -= 1
    k = j
    while k > 0 and s[k - 1].lower() in _NAME_CHARS:
        k -= 1
    return s[k:j].lower()


def rewrite_avg(sql: str) -> str:
    """Rewrite unqualified ``avg(`` calls in ``sql`` to ``pg_oblako.avg(``."""
    if "avg" not in sql.lower():
        return sql
    out: list[str] = []
    i, n, last = 0, len(sql), 0
    while i < n:
        c = sql[i]
        if c in ("'", '"'):
            i = _skip_quoted(sql, i, c)
            continue
        if sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if sql.startswith("/*", i):
            close = sql.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        if sql[i : i + 3].lower() == "avg":
            before = sql[i - 1] if i > 0 else " "
            after = i + 3
            j = after
            while j < n and sql[j].isspace():
                j += 1
            standalone = before.lower() not in _NAME_CHARS and before != "."
            is_call = (
                j < n
                and sql[j] == "("
                and (after >= n or sql[after].lower() not in _NAME_CHARS)
            )
            definition = _previous_word(sql, i) in ("function", "aggregate")
            if standalone and is_call and not definition:
                out.append(sql[last:i])
                out.append(_TARGET)
                last = after
            i = after
            continue
        i += 1
    out.append(sql[last:])
    return "".join(out)
