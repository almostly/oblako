"""Integration: redshift-local is compatible with Redshift access tools (redtape).

redtape (tomasfarias/redtape, MIT) manages Redshift users/groups/privileges as
code. It connects with psycopg2 and introspects a forked pg_catalog + Redshift-only
views/functions. This checks the exact dependencies its introspection needs, so
redtape's `export`/`run` work against oblako's redshift-local unchanged.

Requires the engine (docker compose up redshift). Applies the shipped
`05_catalog_views.sql` (idempotent), then exercises the compat layer through the
proxy on 5439.
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

# redtape's exact introspection queries. If these run, redtape can read oblako.
REDTAPE_USERS_Q = (
    "SELECT usename, usesysid, usecreatedb, usesuper, usecatupd, valuntil, useconfig "
    "FROM pg_catalog.pg_user"
)
REDTAPE_GROUPS_Q = "SELECT groname, grosysid, grolist FROM pg_catalog.pg_group"


@pytest.fixture
def cursor():
    """Autocommit cursor on the proxy (5439) with the catalog views applied."""
    conn = psycopg2.connect(**RS_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(SQL)  # idempotent CREATE OR REPLACE of the compat views/functions
    yield cur
    conn.close()


def test_redtape_users_query_runs(cursor):
    """redtape's user query runs; the proxy answers the Redshift-only usecatupd."""
    cursor.execute(REDTAPE_USERS_Q)
    assert "usecatupd" in [d[0] for d in cursor.description]


def test_redtape_groups_query_runs(cursor):
    """redtape's group query runs on the native pg_group (groname/grosysid/grolist)."""
    cursor.execute(REDTAPE_GROUPS_Q)
    assert [d[0] for d in cursor.description] == ["groname", "grosysid", "grolist"]


def test_pg_group_excludes_predefined_roles(cursor):
    """Through the proxy, pg_group shows no PostgreSQL predefined pg_* roles."""
    cursor.execute("SELECT groname FROM pg_catalog.pg_group")
    names = [r[0] for r in cursor.fetchall()]
    assert not any(n.startswith("pg_") for n in names)


def test_like_escape(cursor):
    """like_escape() converts a custom LIKE escape char to the backslash escape."""
    cursor.execute("SELECT like_escape('pg!_temp!_%', '!')")
    assert cursor.fetchone()[0] == r"pg\_temp\_%"


def test_svv_external_schemas_has_eskind(cursor):
    """svv_external_schemas exposes eskind (redtape reads it) and is empty."""
    cursor.execute(
        "SELECT esoid, schemaname, databasename, esoptions, esowner, eskind "
        "FROM svv_external_schemas"
    )
    assert cursor.fetchall() == []


def test_external_schema_functions_are_empty(cursor):
    """The data-sharing / external-schema set-functions exist and return no rows."""
    for fn, ncols in [
        ("pg_get_shared_redshift_schemas", 6),
        ("pg_get_all_external_schemas", 7),
    ]:
        cols = ", ".join(f"c{i} text" for i in range(ncols))
        cursor.execute(f"SELECT count(*) FROM {fn}() AS r({cols})")
        assert cursor.fetchone()[0] == 0


def test_createuser_privilege_accepted(cursor):
    """CREATE USER ... CREATEUSER (Redshift) is accepted as a SUPERUSER via the proxy."""
    cursor.execute("DROP USER IF EXISTS redtape_su_probe")
    cursor.execute("CREATE USER redtape_su_probe PASSWORD 'Probe_123' CREATEUSER")
    cursor.execute("SELECT usesuper FROM pg_user WHERE usename = 'redtape_su_probe'")
    assert cursor.fetchone()[0] is True
    cursor.execute("DROP USER redtape_su_probe")


def test_public_schema_owned_by_a_real_user(cursor):
    """public is owned by a user (not PG15's pg_database_owner) so owner->user maps."""
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_user u JOIN pg_namespace n "
        "ON n.nspowner = u.usesysid WHERE n.nspname = 'public')"
    )
    assert cursor.fetchone()[0] is True
