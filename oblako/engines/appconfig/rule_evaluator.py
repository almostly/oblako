"""AppConfig feature-flag rule evaluator — Python port of the AWS AppConfig agent.

Ported from the AppConfig agent (a Python re-implementation of Amazon's Go
GoAmzn-AWSAppConfigRuleEvaluator). It parses and evaluates the S-expression rules
attached to feature-flag variants, exactly like the agent that runs as a Lambda
extension / sidecar against real AWS — so flags resolve identically against oblako.

Go -> Python mapping:
  rules.(*expressionBuilder).buildSexp     -> _parse()
  rules.(*contextExpression).eval          -> ('$', name) context lookup
  rules.(*staticExpression[T]).eval        -> str/int/float/bool literal
  rules.(*comparisonExpression).eval       -> eq / gt / lt / gte / lte
  rules.(*variadicLogicExpression).eval    -> and / or
  rules.(*notExpression).eval              -> not
  rules.(*inExpression).eval               -> in
  rules.(*existsExpression).eval           -> exists
  rules.(*stringMatchExpression).eval      -> begins_with / ends_with / contains
  rules.(*splitExpression).eval            -> split (FNV-1a percentage bucketing)
  rules.(*regexpExpression).eval           -> matches
  rules.(*Renderer).RenderJson             -> evaluate_config()
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Any

LOG = logging.getLogger(__name__)

# Keys that are AppConfig metadata, not user-defined flag attributes.
_META_KEYS = frozenset[str](
    {
        "_variant",
        "_createdAt",
        "_updatedAt",
        "enabled",
        "name",
        "description",
        "_variants",
        "attributes",
        "attributeValues",
    }
)


# Tokenizer — rules.(*expressionBuilder).buildSexp (lexing)
_TOKEN_RE = re.compile(
    r"\(|\)"  # parens
    r'|"(?:[^"\\]|\\.)*"'  # quoted strings
    r"|\'(?:[^\'\\]|\\.)*\'"  # single-quoted strings
    r"|-?\d+\.\d+"  # floats
    r"|-?\d+"  # ints
    r"|true|false|null"  # literals
    r"|\$[\w.]+"  # context variables: $foo, $foo.bar
    r"|[\w_][\w_.:-]*"  # operators and identifiers
)


def _tokenize(expr: str) -> list[str]:
    return _TOKEN_RE.findall(expr)


# Parser — rules.(*expressionBuilder).buildSexp (parse). Output:
#   list                    -> S-expression: [operator, arg1, arg2, ...]
#   ('$', name)             -> context variable reference
#   str/int/float/bool/None -> static literal
def _parse_tokens(tokens: list[str], pos: int) -> tuple[Any, int]:
    if pos >= len(tokens):
        raise ValueError("unexpected end of expression")

    token = tokens[pos]

    if token == "(":
        pos += 1
        items: list[Any] = []
        while pos < len(tokens) and tokens[pos] != ")":
            item, pos = _parse_tokens(tokens, pos)
            items.append(item)
        if pos >= len(tokens):
            raise ValueError("missing closing ')'")
        pos += 1  # consume ')'
        return items, pos

    if token == ")":
        raise ValueError("unexpected ')'")

    if token.startswith('"') or token.startswith("'"):
        return token[1:-1].replace('\\"', '"').replace("\\'", "'"), pos + 1

    if token == "true":
        return True, pos + 1
    if token == "false":
        return False, pos + 1
    if token == "null":
        return None, pos + 1

    if token.startswith("$"):
        return ("$", token[1:]), pos + 1

    with contextlib.suppress(ValueError):
        return (float(token), pos + 1) if "." in token else (int(token), pos + 1)
    return token, pos + 1


def _parse(expr: str) -> Any:
    """Parse an S-expression rule string into a nested Python structure."""
    tokens = _tokenize(expr.strip())
    if not tokens:
        raise ValueError(f"empty expression: {expr!r}")
    node, consumed = _parse_tokens(tokens, 0)
    if consumed != len(tokens):
        raise ValueError(f"trailing tokens in expression: {tokens[consumed:]}")
    return node


def _fnv1a_32(data: bytes) -> int:
    """FNV-1a 32-bit hash (Go hash/fnv.sum32a) — deterministic split bucketing."""
    h = 0x811C9DC5  # FNV offset basis
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF  # FNV prime
    return h


def _coerce(value: Any, target: Any) -> Any:
    """Coerce a context string to the type of the rule literal (safeFloat/String/Bool)."""
    if not isinstance(value, str):
        return value
    if isinstance(target, bool):
        return value.lower() in ("true", "1", "yes")
    if isinstance(target, int):
        with contextlib.suppress(ValueError, TypeError):
            return int(value)
    if isinstance(target, float):
        with contextlib.suppress(ValueError, TypeError):
            return float(value)
    return value


def _resolve(node: Any, context: dict[str, Any], coerce_to: Any = None) -> Any:
    """Resolve a context variable or return a static value."""
    if isinstance(node, tuple) and node[0] == "$":
        val = context.get(node[1])
        return _coerce(val, coerce_to) if coerce_to is not None else val
    return node


def _eval(node: Any, context: dict[str, Any]) -> Any:
    """Recursively evaluate a parsed S-expression node against a context dict."""
    if not isinstance(node, (list, tuple)):
        return node

    if isinstance(node, tuple) and node[0] == "$":
        return context.get(node[1])

    if not isinstance(node, list) or not node:
        return None

    op = node[0]
    args = node[1:]

    if op == "and":
        return all(_eval(a, context) for a in args)
    if op == "or":
        return any(_eval(a, context) for a in args)
    if op == "not":
        return not _eval(args[0], context)

    # Comparisons — coerce the context string to the literal's type first.
    if op == "eq":
        right = _resolve(args[1], context)
        left = _resolve(args[0], context, coerce_to=right)
        return left == right
    if op == "gt":
        right = _resolve(args[1], context)
        left = _resolve(args[0], context, coerce_to=right)
        return left is not None and right is not None and left > right
    if op == "lt":
        right = _resolve(args[1], context)
        left = _resolve(args[0], context, coerce_to=right)
        return left is not None and right is not None and left < right
    if op == "gte":
        right = _resolve(args[1], context)
        left = _resolve(args[0], context, coerce_to=right)
        return left is not None and right is not None and left >= right
    if op == "lte":
        right = _resolve(args[1], context)
        left = _resolve(args[0], context, coerce_to=right)
        return left is not None and right is not None and left <= right

    if op == "in":
        val = _resolve(args[0], context)
        collection = [_resolve(a, context) for a in args[1:]]
        return val in collection

    if op == "exists":
        inner = args[0]
        if isinstance(inner, tuple) and inner[0] == "$":
            return inner[1] in context
        return inner is not None

    if op == "begins_with":
        val = _resolve(args[0], context)
        prefix = _resolve(args[1], context)
        return isinstance(val, str) and isinstance(prefix, str) and val.startswith(prefix)
    if op == "ends_with":
        val = _resolve(args[0], context)
        suffix = _resolve(args[1], context)
        return isinstance(val, str) and isinstance(suffix, str) and val.endswith(suffix)
    if op == "contains":
        val = _resolve(args[0], context)
        substr = _resolve(args[1], context)
        return isinstance(val, str) and isinstance(substr, str) and substr in val

    # Percentage split: (split by:: $var pct::N seed:: "str")
    # Confirmed from the Go binary: fnv1a_32(val + seed) % 10000 < pct * 100
    if op == "split":
        by_val = None
        pct = None
        seed = ""
        i = 0
        while i < len(args):
            a = args[i]
            if isinstance(a, str) and a == "by::":
                if i + 1 < len(args):
                    by_val = _resolve(args[i + 1], context)
                    i += 2
                    continue
            elif isinstance(a, str) and a.startswith("pct::"):
                pct = int(a[5:])
            elif isinstance(a, str) and a == "seed::":
                if i + 1 < len(args):
                    seed = _resolve(args[i + 1], context) or ""
                    i += 2
                    continue
            elif isinstance(a, str) and a.startswith("seed::"):
                seed = a[6:]
            i += 1
        if by_val is None or pct is None:
            return False
        bucket = _fnv1a_32(f"{by_val}{seed}".encode()) % 10000
        return bucket < pct * 100

    if op in ("matches", "match"):
        val = _resolve(args[0], context)
        pattern = _resolve(args[1], context)
        if isinstance(val, str) and isinstance(pattern, str):
            return bool(re.search(pattern, val))
        return False

    LOG.warning("unknown rule operator: %r", op)
    return False


def evaluate_rule(rule: str, context: dict[str, Any]) -> bool:
    """Parse and evaluate a single S-expression rule string against a context."""
    try:
        node = _parse(rule)
        return bool(_eval(node, context))
    except Exception as exc:  # noqa: BLE001 - a bad rule never breaks evaluation
        LOG.warning("rule evaluation failed for %r: %s", rule, exc)
        return False


def evaluate_config(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a raw AppConfig feature-flag ``values`` map against a context.

    Mirrors rules.(*Renderer).RenderJson: for each enabled flag, evaluate each
    variant's rule against the context, set ``_variant`` to the first match, and
    promote that variant's ``attributeValues`` to the flag root. Disabled flags
    are dropped. A variant with no rule is the default (always matches).
    """
    result: dict[str, Any] = {}
    for flag_name, flag_value in config.items():
        if not isinstance(flag_value, dict):
            result[flag_name] = flag_value
            continue
        if not flag_value.get("enabled", True):
            continue

        evaluated = dict(flag_value)
        for variant in flag_value.get("_variants", []):
            if not variant.get("enabled", True):
                continue
            rule = variant.get("rule")
            if rule is None or evaluate_rule(rule, context):
                evaluated["_variant"] = variant.get("name")
                attrs = variant.get("attributeValues", variant.get("attributes", {}))
                for k, v in attrs.items():
                    evaluated[k] = v
                break
        result[flag_name] = evaluated
    return result


def extract_attributes(flag_value: dict[str, Any]) -> dict[str, Any]:
    """Return a flag's user-defined attributes, stripping AppConfig metadata."""
    return {k: v for k, v in flag_value.items() if k not in _META_KEYS}
