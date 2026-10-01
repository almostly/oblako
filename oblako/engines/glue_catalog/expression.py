"""Glue partition filter expressions (``GetPartitions(Expression=...)``).

The SQL-like subset Glue documents: ``=``, ``<>`` / ``!=``, ``<``, ``<=``,
``>``, ``>=``, ``BETWEEN``, ``IN``, ``LIKE``, ``IS [NOT] NULL``, ``AND``,
``OR``, ``NOT`` and parentheses, over partition keys and string or number
literals. Keys with a numeric type compare as numbers, the rest as strings.
"""

from __future__ import annotations

import re
from typing import Callable

Predicate = Callable[[dict], bool]

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')"
    r"|(?P<num>-?\d+(?:\.\d+)?)(?![A-Za-z_])"
    r"|(?P<op><>|!=|>=|<=|=|<|>|\(|\)|,)"
    r"|(?P<ident>`[^`]+`|\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*))"
)
_NUMERIC = (
    "tinyint",
    "smallint",
    "int",
    "integer",
    "bigint",
    "float",
    "double",
    "decimal",
)
_KEYWORDS = {"and", "or", "not", "between", "in", "like", "is", "null"}


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text.rstrip()):
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise ValueError(f"unexpected input at {pos}: {text[pos : pos + 20]!r}")
        kind = match.lastgroup or ""
        value = match.group(kind)
        if kind == "str":
            value = value[1:-1].replace("''", "'")
        elif kind == "ident":
            if value[0] in '`"':
                value = value[1:-1]
            elif value.lower() in _KEYWORDS:
                kind, value = "kw", value.lower()
        out.append((kind, value))
        pos = match.end()
    return out


class _Parser:
    def __init__(self, text: str, types: dict[str, str]):
        self.tokens = _tokens(text)
        self.pos = 0
        self.types = {k.lower(): v.lower() for k, v in types.items()}

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        i = self.pos + offset
        return self.tokens[i] if i < len(self.tokens) else None

    def take(self, kind: str | None = None, value: str | None = None) -> str:
        token = self.peek()
        if (
            token is None
            or (kind and token[0] != kind)
            or (value and token[1] != value)
        ):
            raise ValueError(f"expected {value or kind}, got {token}")
        self.pos += 1
        return token[1]

    def accept(self, kind: str, value: str) -> bool:
        if self.peek() == (kind, value):
            self.pos += 1
            return True
        return False

    def parse(self) -> Predicate:
        pred = self.or_expr()
        if self.peek() is not None:
            raise ValueError(f"unexpected {self.peek()}")
        return pred

    def or_expr(self) -> Predicate:
        parts = [self.and_expr()]
        while self.accept("kw", "or"):
            parts.append(self.and_expr())
        return parts[0] if len(parts) == 1 else (lambda row: any(p(row) for p in parts))

    def and_expr(self) -> Predicate:
        parts = [self.not_expr()]
        while self.accept("kw", "and"):
            parts.append(self.not_expr())
        return parts[0] if len(parts) == 1 else (lambda row: all(p(row) for p in parts))

    def not_expr(self) -> Predicate:
        if self.accept("kw", "not"):
            inner = self.not_expr()
            return lambda row: not inner(row)
        if self.accept("op", "("):
            inner = self.or_expr()
            self.take("op", ")")
            return inner
        return self.comparison()

    def literal(self) -> str:
        kind, value = self.peek() or ("", "")
        if kind not in ("str", "num"):
            raise ValueError(f"expected a literal, got {self.peek()}")
        self.pos += 1
        return value

    def comparison(self) -> Predicate:
        key = self.take("ident").lower()
        numeric = self.types.get(key, "string").startswith(_NUMERIC)

        def val(raw):
            if raw is None:
                return None
            return float(raw) if numeric else str(raw)

        def get(row):
            return val(row.get(key))

        if self.accept("kw", "is"):
            negate = self.accept("kw", "not")
            self.take("kw", "null")
            return lambda row: (row.get(key) is None) != negate
        negate = self.accept("kw", "not")
        if self.accept("kw", "between"):
            low = val(self.literal())
            self.take("kw", "and")
            high = val(self.literal())
            return lambda row: (
                (get(row) is not None and low <= get(row) <= high) != negate
            )
        if self.accept("kw", "in"):
            self.take("op", "(")
            options = {val(self.literal())}
            while self.accept("op", ","):
                options.add(val(self.literal()))
            self.take("op", ")")
            return lambda row: (get(row) in options) != negate
        if self.accept("kw", "like"):
            pattern = re.escape(self.literal()).replace("%", ".*").replace("_", ".")
            regex = re.compile(pattern, re.DOTALL)
            return lambda row: (
                (
                    row.get(key) is not None
                    and regex.fullmatch(str(row[key])) is not None
                )
                != negate
            )
        if negate:
            raise ValueError("NOT must precede BETWEEN, IN or LIKE here")
        op = self.take("op")
        right = val(self.literal())
        compare = {
            "=": lambda a: a == right,
            "<>": lambda a: a != right,
            "!=": lambda a: a != right,
            "<": lambda a: a < right,
            "<=": lambda a: a <= right,
            ">": lambda a: a > right,
            ">=": lambda a: a >= right,
        }.get(op)
        if compare is None:
            raise ValueError(f"unknown operator {op}")
        return lambda row: get(row) is not None and compare(get(row))


def compile_expression(text: str, types: dict[str, str]) -> Predicate:
    """Compile a partition filter; ``types`` maps partition keys to Glue types.

    The predicate takes ``{key: value}`` for one partition. Raises ValueError
    on an expression outside the supported subset.
    """
    return _Parser(text, types).parse()
