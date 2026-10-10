"""Integration: quoted identifiers fold to lower case, as on Redshift.

Requires the engine (docker compose up redshift). Redshift folds a quoted
database, schema, table or column name to lower case unless the session sets
enable_case_sensitive_identifier; a quoted user name keeps its case. Override the
port with OBLAKO_TEST_RS_PORT to run against an isolated stack.
"""

import os

import psycopg2
import pytest

RS_PORT = int(os.environ.get("OBLAKO_TEST_RS_PORT", "5439"))
RS_CONFIG = dict(
    host="localhost", port=RS_PORT, user="oblako", password="oblako", dbname="oblako"
)


@pytest.fixture
def cursor():
    conn = psycopg2.connect(**RS_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('DROP SCHEMA IF EXISTS "Idf_Mixed Schema" CASCADE')
    cur.execute('DROP USER IF EXISTS "IDF_Mixed"')
    yield cur
    cur.execute('DROP SCHEMA IF EXISTS "Idf_Mixed Schema" CASCADE')
    cur.execute("SET enable_case_sensitive_identifier TO true")  # "Idf_Kept" as made
    cur.execute('DROP SCHEMA IF EXISTS "Idf_Kept" CASCADE')
    cur.execute("RESET enable_case_sensitive_identifier")
    cur.execute('DROP USER IF EXISTS "IDF_Mixed"')
    conn.close()


def test_quoted_names_are_stored_folded(cursor):
    """As on Serverless: "pp_Mixed Schema" is stored as pp_mixed schema."""
    cursor.execute('CREATE SCHEMA "Idf_Mixed Schema"')
    cursor.execute('CREATE TABLE "Idf_Mixed Schema"."Odd Table" ("Id" int)')
    cursor.execute(
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname ILIKE 'idf_mixed schema'"
    )
    assert cursor.fetchall() == [("idf_mixed schema", "odd table")]
    cursor.execute('SELECT "ID" AS "MyAlias" FROM "IDF_MIXED SCHEMA"."ODD TABLE"')
    assert [d[0] for d in cursor.description] == ["myalias"]


def test_a_quoted_user_name_keeps_its_case(cursor):
    cursor.execute("CREATE USER \"IDF_Mixed\" PASSWORD 'Abcdef12'")
    cursor.execute("SELECT usename FROM pg_user WHERE usename ILIKE 'idf_mixed'")
    assert cursor.fetchall() == [("IDF_Mixed",)]


def test_enable_case_sensitive_identifier_keeps_the_case(cursor):
    cursor.execute("SET enable_case_sensitive_identifier TO true")
    cursor.execute('CREATE SCHEMA "Idf_Kept"')
    cursor.execute("SELECT nspname FROM pg_namespace WHERE nspname ILIKE 'idf_kept'")
    assert cursor.fetchall() == [("Idf_Kept",)]


def test_names_run_to_127_bytes(cursor):
    """As on Redshift: names up to 127 bytes, not PostgreSQL's 63."""
    name = "idf_" + "n" * 123
    cursor.execute("SHOW max_identifier_length")
    assert cursor.fetchone() == ("127",)
    cursor.execute(f"DROP USER IF EXISTS {name}")
    cursor.execute(f"CREATE USER {name} PASSWORD 'Abcdef12'")
    try:
        cursor.execute(
            "SELECT length(usename) FROM pg_user WHERE usename LIKE 'idf_nnn%'"
        )
        assert cursor.fetchone() == (127,)
    finally:
        cursor.execute(f"DROP USER {name}")
