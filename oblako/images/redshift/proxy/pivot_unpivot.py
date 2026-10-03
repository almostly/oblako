"""Rewrite Redshift ``PIVOT`` / ``UNPIVOT`` into standard SQL, for the wire proxy.

PostgreSQL has no PIVOT/UNPIVOT, and sqlglot (which parses them into an AST) can't
emit them to Postgres either - it silently drops them. But their standard-SQL
equivalents are well known, so this uses sqlglot only as a *parser* and does the
transform itself:

    PIVOT   (agg(v) FOR c IN (x, y))   -> agg(CASE WHEN c = x THEN v END) ... GROUP BY <rest>
    UNPIVOT (v FOR n IN (a, b))        -> CROSS JOIN LATERAL (VALUES ('a', a), ...) u(n, v)

The transform needs the source's column list (to know the group-by / kept columns),
which is only available when the source is a subquery / CTE - a bare-table source
has no schema here, so it's left untouched (PostgreSQL then errors, as before).
Single aggregate only for PIVOT. Gated on the PIVOT keyword, and any parse/transform
failure falls back to the original SQL, so ordinary queries are never affected.

sqlglot is an optional dependency; if it's absent the proxy simply doesn't rewrite.
"""

from __future__ import annotations

import importlib.util

# sqlglot is optional: without it the proxy does not rewrite
HAVE_SQLGLOT = importlib.util.find_spec("sqlglot") is not None
if HAVE_SQLGLOT:
    import sqlglot
    from sqlglot import exp


def _source_columns(parent) -> list[str] | None:
    """Output column names of a pivot's source, or None if not determinable."""
    if not isinstance(parent, exp.Subquery):
        return None  # bare table / unknown -> no schema in the proxy
    inner = parent.this
    if not isinstance(inner, exp.Select):
        return None
    cols: list[str] = []
    for e in inner.expressions:
        name = e.alias_or_name
        if not name or name == "*":
            return None  # SELECT * -> columns unknown
        cols.append(name)
    return cols


def _build_pivot(piv, source_sql: str, cols: list[str]) -> str | None:
    """conditional-aggregation subquery for a PIVOT (single aggregate only)."""
    aggs = piv.expressions
    if len(aggs) != 1:
        return None
    agg = aggs[0].unalias() if isinstance(aggs[0], exp.Alias) else aggs[0]
    arg = agg.this  # the aggregated column
    arg_name = arg.name if isinstance(arg, exp.Column) else None
    func = agg.sql_name()  # e.g. 'AVG'
    field = piv.args["fields"][0]  # exp.In: FOR <col> IN (...)
    for_col = field.this.name
    group_cols = [c for c in cols if c != for_col and c != arg_name]

    projections = list(group_cols)
    for item in field.expressions:
        value = item.unalias() if isinstance(item, exp.Alias) else item
        alias = item.alias if isinstance(item, exp.Alias) else value.name
        projections.append(
            f"{func}(CASE WHEN {for_col} = {value.sql(dialect='postgres')} "
            f'THEN {arg.sql(dialect="postgres")} END) AS "{alias}"'
        )
    group_by = f" GROUP BY {', '.join(group_cols)}" if group_cols else ""
    return f"(SELECT {', '.join(projections)} FROM {source_sql}{group_by}) AS __piv"


def _build_unpivot(piv, parent, source_sql: str, cols: list[str]) -> str | None:
    """CROSS JOIN LATERAL (VALUES ...) for an UNPIVOT."""
    value_col = piv.expressions[0].name  # UNPIVOT (<value> FOR <name> IN ...)
    field = piv.args["fields"][0]
    name_col = field.this.name
    in_cols = [e.name for e in field.expressions]
    alias = parent.alias or "src"
    kept = [f"{alias}.{c}" for c in cols if c not in in_cols]
    rows = ", ".join(f"('{c}', {alias}.{c})" for c in in_cols)
    select_cols = ", ".join([*kept, f"u.{name_col}", f"u.{value_col}"])
    # Redshift UNPIVOT excludes NULLs by default.
    return (
        f"(SELECT {select_cols} FROM {source_sql} "
        f"CROSS JOIN LATERAL (VALUES {rows}) AS u({name_col}, {value_col}) "
        f"WHERE u.{value_col} IS NOT NULL) AS __unpiv"
    )


def rewrite_pivot_unpivot(sql: str) -> str:
    """Rewrite PIVOT/UNPIVOT over a subquery source into standard SQL."""
    if not HAVE_SQLGLOT or "pivot" not in sql.lower():
        return sql
    try:
        tree = sqlglot.parse_one(sql, read="redshift")
    except Exception:  # unparseable -> leave untouched
        return sql
    pivots = list(tree.find_all(exp.Pivot))
    if not pivots:
        return sql
    replaced = 0
    for piv in pivots:
        parent = piv.parent
        if parent is None:
            continue
        cols = _source_columns(parent)
        if cols is None:
            continue
        try:
            piv.pop()  # detach the pivot so source_sql is the plain source
            source_sql = parent.sql(dialect="postgres")
            if piv.args.get("unpivot"):
                new_sql = _build_unpivot(piv, parent, source_sql, cols)
            else:
                new_sql = _build_pivot(piv, source_sql, cols)
            if new_sql is None:
                continue
            parent.replace(sqlglot.parse_one(new_sql, read="postgres"))
            replaced += 1
        except Exception:  # transform failed -> leave this one
            continue
    if not replaced:
        return sql
    try:
        return tree.sql(dialect="postgres")
    except Exception:
        return sql
