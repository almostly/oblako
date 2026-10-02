"""Athena data catalogs and the Trino catalogs that serve them.

``AwsDataCatalog`` is Trino's ``awsdatacatalog`` (the Hive connector over oblako's
Glue engine). S3 Tables are queried on AWS through a catalog per table bucket,
``s3tablescatalog/<bucket>``, with the table bucket's namespace as the database.
Locally every table bucket lives in the one Iceberg REST catalog as the first
level of a two-level namespace, which Trino (with nested namespaces on) shows as
the schema ``<bucket>.<namespace>`` of its ``iceberg`` catalog. This module maps
one onto the other, both for the query context and for fully qualified names in
the SQL text.
"""

from __future__ import annotations

import re

DEFAULT_CATALOG = "awsdatacatalog"  # Trino's Hive-over-Glue catalog
_S3TABLES = "s3tablescatalog/"

# "s3tablescatalog/<bucket>"."<namespace>"  (the namespace quoted or bare)
_QUALIFIED = re.compile(
    r'"s3tablescatalog/([^"]+)"\s*\.\s*(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))',
    re.IGNORECASE,
)


def resolve(catalog: str | None, database: str | None) -> tuple[str, str | None]:
    """Return the Trino (catalog, schema) for an Athena query context."""
    name = (catalog or DEFAULT_CATALOG).lower()
    if name.startswith(_S3TABLES):
        bucket = name[len(_S3TABLES) :]
        return "iceberg", f"{bucket}.{database.lower()}" if database else None
    return name, database


def rewrite(sql: str) -> str:
    """Point fully qualified S3 Tables names in ``sql`` at Trino's iceberg catalog."""

    def to_iceberg(match: re.Match) -> str:
        bucket = match.group(1).lower()
        namespace = (match.group(2) or match.group(3)).lower()
        return f'iceberg."{bucket}.{namespace}"'

    return _QUALIFIED.sub(to_iceberg, sql)
