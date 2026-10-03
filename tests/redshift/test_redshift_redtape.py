"""Integration: redshift-local is compatible with Redshift access tools (redtape).

redtape (tomasfarias/redtape, MIT) manages Redshift users/groups/privileges as
code. It connects with psycopg2 and introspects a forked pg_catalog + Redshift-only
views/functions. This checks the exact dependencies its introspection needs, so
redtape's `export`/`run` work against oblako's redshift-local unchanged.

Requires the engine (docker compose up redshift). Applies the shipped
`05_catalog_views.sql` (idempotent), then exercises the compat layer through the
proxy, on 5439 unless OBLAKO_TEST_RS_PORT points it at an isolated stack.
"""

import os
from pathlib import Path

import psycopg2
import pytest

RS_PORT = int(os.environ.get("OBLAKO_TEST_RS_PORT", "5439"))
RS_CONFIG = dict(
    host="localhost", port=RS_PORT, user="oblako", password="oblako", dbname="oblako"
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


def test_password_disable_creates_a_passwordless_user(cursor):
    """CREATE/ALTER USER ... PASSWORD DISABLE (Redshift's IAM-only account) works.

    Redshift provisions an account that holds grants but cannot authenticate with a
    password; PostgreSQL spells that PASSWORD NULL, which the proxy rewrites to.
    """
    cursor.execute("DROP USER IF EXISTS redtape_iam_only")
    cursor.execute("CREATE USER redtape_iam_only PASSWORD DISABLE")
    cursor.execute(
        "SELECT passwd IS NULL FROM pg_shadow WHERE usename = 'redtape_iam_only'"
    )
    assert cursor.fetchone()[0] is True
    cursor.execute("ALTER USER redtape_iam_only PASSWORD DISABLE")
    cursor.execute("DROP USER redtape_iam_only")


def test_public_schema_owned_by_a_real_user(cursor):
    """public is owned by a user (not PG15's pg_database_owner) so owner->user maps."""
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_user u JOIN pg_namespace n "
        "ON n.nspowner = u.usesysid WHERE n.nspname = 'public')"
    )
    assert cursor.fetchone()[0] is True


# redtape's exact ACL reads (connectors.py:34, :68). The proxy points these at
# redshift_acl(), which renders a group grantee the way Redshift does.
REDTAPE_SCHEMA_ACL_Q = (
    "SELECT array_to_string(pgn.nspacl, (',')::text)::TEXT AS schema_acl "
    "FROM pg_namespace pgn WHERE pgn.nspname = 'redtape_sales'"
)
REDTAPE_TABLE_ACL_Q = (
    "SELECT array_to_string(pgc.relacl, ','::text)::TEXT AS table_acl "
    "FROM pg_class pgc WHERE pgc.relname = 'redtape_orders'"
)


def parse_acl_holder(acl_str):
    """redtape's ACL-holder branch (connectors.py:283-293), verbatim in behaviour.

    Returns (holder_name, holder_type) for a single `grantee=privs/grantor` entry.
    """
    acl_str, _, _ = acl_str.partition("/")
    user_or_group, _, _actions = acl_str.partition("=")
    if not user_or_group:
        return "PUBLIC", "PUBLIC"
    if user_or_group.startswith("group"):
        return user_or_group.split(" ")[1], "group"
    return user_or_group, "user"


@pytest.fixture
def group_grant(cursor):
    """A group and a user each granted on one schema + table, dropped afterwards."""
    cursor.execute("DROP SCHEMA IF EXISTS redtape_sales CASCADE")
    cursor.execute("DROP GROUP IF EXISTS redtape_analysts")
    cursor.execute("DROP USER IF EXISTS redtape_bi")
    cursor.execute("CREATE GROUP redtape_analysts")
    cursor.execute("CREATE USER redtape_bi PASSWORD 'Analyst_1'")
    cursor.execute("CREATE SCHEMA redtape_sales")
    cursor.execute("CREATE TABLE redtape_sales.redtape_orders (id int)")
    cursor.execute("GRANT USAGE ON SCHEMA redtape_sales TO GROUP redtape_analysts")
    cursor.execute(
        "GRANT SELECT ON redtape_sales.redtape_orders TO GROUP redtape_analysts"
    )
    cursor.execute("GRANT SELECT ON redtape_sales.redtape_orders TO redtape_bi")
    yield cursor
    cursor.execute("DROP SCHEMA IF EXISTS redtape_sales CASCADE")
    cursor.execute("DROP GROUP IF EXISTS redtape_analysts")
    cursor.execute("DROP USER IF EXISTS redtape_bi")


def test_group_grant_acl_uses_redshift_group_prefix(group_grant):
    """Group grantees read back prefixed, as Redshift renders them, not bare."""
    for query in (REDTAPE_SCHEMA_ACL_Q, REDTAPE_TABLE_ACL_Q):
        group_grant.execute(query)
        acl = group_grant.fetchone()[0]
        assert "group redtape_analysts=" in acl


def test_user_grant_acl_has_no_group_prefix(group_grant):
    """A user grantee stays bare, so only real groups pick up the prefix."""
    group_grant.execute(REDTAPE_TABLE_ACL_Q)
    entries = group_grant.fetchone()[0].split(",")
    user = [e for e in entries if e.startswith("redtape_bi=")]
    assert user and not any(e.startswith("group redtape_bi=") for e in entries)


def test_acl_parses_as_a_group_not_a_user(group_grant):
    """The loop redtape actually runs: grant to a group, re-read, still a group.

    Both reads, because redtape diffs schema privileges through a query of its own,
    and a group USAGE grant on a schema renders bare in PostgreSQL exactly as a
    table grant does. Without the prefix these return ("redtape_analysts", "user"),
    the group reads as holding nothing, and redtape re-plans the same GRANT forever.
    """
    for query in (REDTAPE_SCHEMA_ACL_Q, REDTAPE_TABLE_ACL_Q):
        group_grant.execute(query)
        holders = dict(
            parse_acl_holder(e) for e in group_grant.fetchone()[0].split(",")
        )
        assert holders["redtape_analysts"] == "group"
    assert holders["redtape_bi"] == "user"  # the user grant is on the table only


def test_compat_layer_reaches_the_postgres_database():
    """Tools that walk pg_database and reconnect (redtape export) hit `postgres`.

    initdb creates it before the init scripts run, so template1 does not reach it;
    06 seeds it directly. Without this, the ACL read fails there.
    """
    conn = psycopg2.connect(**{**RS_CONFIG, "dbname": "postgres"})
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regprocedure('pg_catalog.redshift_acl(aclitem[],text)') IS NOT NULL,"
            " to_regclass('pg_catalog.svv_external_schemas') IS NOT NULL"
        )
        assert cur.fetchone() == (True, True)
    conn.close()
