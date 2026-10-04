"""Integration tests for the Redshift catalog views (pg_table_def, svv_*).

Requires the engine (docker compose up redshift). Applies the shipped
`05_catalog_views.sql` (idempotent CREATE OR REPLACE VIEW), creates a sample
table, and checks the views BI tools / dbt query for metadata.
"""

from pathlib import Path

import psycopg2
import pytest

RS_CONFIG = dict(
    host="localhost", port=5439, user="oblako", password="oblako", dbname="oblako"
)
SQL = (
    Path(__file__).parents[2] / "oblako/images/redshift/initdb.d/05_catalog_views.sql"
).read_text()
TABLE = "catalog_probe"


@pytest.fixture
def cursor():
    c = psycopg2.connect(**RS_CONFIG)
    c.autocommit = True
    cur = c.cursor()
    cur.execute(SQL)  # ensure the views exist
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    cur.execute(
        f"CREATE TABLE {TABLE} (id int NOT NULL, score numeric, segment varchar(20))"
    )
    cur.execute(f"INSERT INTO {TABLE} VALUES (1, 0.9, 'prime')")
    yield cur
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    c.close()


def test_pg_table_def(cursor):
    cursor.execute(
        'SELECT "column", type, "notnull" FROM pg_table_def '
        'WHERE tablename = %s ORDER BY "column"',
        (TABLE,),
    )
    rows = cursor.fetchall()
    assert rows == [
        ("id", "integer", True),
        ("score", "numeric", False),
        ("segment", "character varying(20)", False),
    ]


def test_svv_columns(cursor):
    cursor.execute(
        "SELECT column_name, data_type, is_nullable FROM svv_columns "
        "WHERE table_name = %s ORDER BY ordinal_position",
        (TABLE,),
    )
    assert cursor.fetchall() == [
        ("id", "integer", "NO"),
        ("score", "numeric", "YES"),
        ("segment", "character varying", "YES"),
    ]


def test_svv_tables_lists_the_table(cursor):
    cursor.execute(
        "SELECT table_type FROM svv_tables WHERE table_schema='public' AND table_name=%s",
        (TABLE,),
    )
    assert cursor.fetchone()[0] == "BASE TABLE"


def test_svv_table_info(cursor):
    cursor.execute(f"ANALYZE {TABLE}")
    cursor.execute(
        'SELECT diststyle, tbl_rows FROM svv_table_info WHERE "table" = %s', (TABLE,)
    )
    diststyle, rows = cursor.fetchone()
    assert diststyle == "EVEN"
    assert rows >= 0  # never the PG -1 "unknown" sentinel


def test_external_views_list_the_registered_schemas_and_tables(cursor):
    """External schemas and their Iceberg tables, nothing else (see 13_iceberg.sql)."""
    cursor.execute(
        "SELECT (SELECT count(*) FROM svv_external_schemas), "
        "(SELECT count(*) FROM pg_oblako.external_schemas), "
        "(SELECT count(*) FROM svv_external_tables), "
        "(SELECT count(*) FROM pg_oblako.iceberg_tables)"
    )
    schemas, registered_schemas, tables, registered_tables = cursor.fetchone()
    assert (schemas, tables) == (registered_schemas, registered_tables)
