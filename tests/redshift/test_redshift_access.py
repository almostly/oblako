"""Integration: redshift-local answers the catalog that Redshift access tools read.

pgsesame (almostly/pgsesame) manages Redshift users, groups, roles and grants as
code. It reads users and groups from pg_user and pg_group, owners joined to
pg_user, and privileges from the SVV views (see test_redshift_identities.py). This
checks the catalog pieces those reads and Redshift's own SQL depend on.

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

# pgsesame's exact user and group reads (pgsesame/redshift.py).
USERS_Q = "select usesysid, usename, usesuper from pg_user"
GROUPS_Q = "select groname, grolist from pg_catalog.pg_group"


@pytest.fixture
def cursor():
    """Autocommit cursor on the proxy (5439) with the catalog views applied."""
    conn = psycopg2.connect(**RS_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(SQL)  # idempotent CREATE OR REPLACE of the compat views/functions
    yield cur
    conn.close()


def test_users_query_runs(cursor):
    """The user read runs on pg_user and finds the admin as a superuser."""
    cursor.execute(USERS_Q)
    assert ("oblako", True) in [(r[1], r[2]) for r in cursor.fetchall()]


def test_groups_query_runs(cursor):
    """The group read runs on the native pg_group (groname/grolist)."""
    cursor.execute(GROUPS_Q)
    assert [d[0] for d in cursor.description] == ["groname", "grolist"]


def test_pg_user_has_usecatupd(cursor):
    """Redshift's pg_user.usecatupd, dropped from PostgreSQL, is answered."""
    cursor.execute(
        "SELECT usename, usesysid, usecreatedb, usesuper, usecatupd, valuntil, "
        "useconfig FROM pg_catalog.pg_user"
    )
    assert "usecatupd" in [d[0] for d in cursor.description]


def test_pg_group_excludes_predefined_roles(cursor):
    """Through the proxy, pg_group shows no PostgreSQL predefined pg_* roles."""
    cursor.execute(GROUPS_Q)
    names = [r[0] for r in cursor.fetchall()]
    assert not any(n.startswith("pg_") for n in names)


def test_svv_external_schemas_has_eskind(cursor):
    """svv_external_schemas has Redshift's eskind: 1, a Data Catalog schema."""
    cursor.execute(
        "SELECT esoid, schemaname, databasename, esoptions, esowner, eskind "
        "FROM svv_external_schemas"
    )
    assert all(row[5] == 1 for row in cursor.fetchall())


def test_createuser_privilege_accepted(cursor):
    """CREATE USER ... CREATEUSER (Redshift) is accepted as a SUPERUSER via the proxy."""
    cursor.execute("DROP USER IF EXISTS acc_su_probe")
    cursor.execute("CREATE USER acc_su_probe PASSWORD 'Probe_123' CREATEUSER")
    cursor.execute("SELECT usesuper FROM pg_user WHERE usename = 'acc_su_probe'")
    assert cursor.fetchone()[0] is True
    cursor.execute("DROP USER acc_su_probe")


def test_password_disable_creates_a_passwordless_user(cursor):
    """CREATE/ALTER USER ... PASSWORD DISABLE (Redshift's IAM-only account) works.

    Redshift provisions an account that holds grants but cannot authenticate with a
    password; PostgreSQL spells that PASSWORD NULL, which the proxy rewrites to.
    pgsesame emits it for a spec's `password: disabled`.
    """
    cursor.execute("DROP USER IF EXISTS acc_iam_only")
    cursor.execute("CREATE USER acc_iam_only PASSWORD DISABLE")
    cursor.execute(
        "SELECT passwd IS NULL FROM pg_shadow WHERE usename = 'acc_iam_only'"
    )
    assert cursor.fetchone()[0] is True
    cursor.execute("ALTER USER acc_iam_only PASSWORD DISABLE")
    cursor.execute("DROP USER acc_iam_only")


def test_public_schema_owned_by_a_real_user(cursor):
    """public is owned by a user (not PG15's pg_database_owner), so owner->user maps.

    pgsesame reads owners joined to pg_user, as Redshift's objects are owned by users.
    """
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_user u JOIN pg_namespace n "
        "ON n.nspowner = u.usesysid WHERE n.nspname = 'public')"
    )
    assert cursor.fetchone()[0] is True


# ACL strings read through array_to_string, as clients write them. The proxy points
# these at redshift_acl(), which renders a group grantee the way Redshift does.
SCHEMA_ACL_Q = (
    "SELECT array_to_string(pgn.nspacl, (',')::text)::TEXT AS schema_acl "
    "FROM pg_namespace pgn WHERE pgn.nspname = 'acc_sales'"
)
TABLE_ACL_Q = (
    "SELECT array_to_string(pgc.relacl, ','::text)::TEXT AS table_acl "
    "FROM pg_class pgc WHERE pgc.relname = 'acc_orders'"
)


def parse_acl_holder(acl_str):
    """Return (holder_name, holder_type) for one Redshift `grantee=privs/grantor` entry."""
    acl_str, _, _ = acl_str.partition("/")
    user_or_group, _, _actions = acl_str.partition("=")
    if not user_or_group:
        return "PUBLIC", "PUBLIC"
    if user_or_group.startswith("group "):
        return user_or_group.split(" ")[1], "group"
    return user_or_group, "user"


@pytest.fixture
def group_grant(cursor):
    """A group and a user each granted on one schema + table, dropped afterwards."""
    cursor.execute("DROP SCHEMA IF EXISTS acc_sales CASCADE")
    cursor.execute("DROP GROUP IF EXISTS acc_analysts")
    cursor.execute("DROP USER IF EXISTS acc_bi")
    cursor.execute("CREATE GROUP acc_analysts")
    cursor.execute("CREATE USER acc_bi PASSWORD 'Analyst_1'")
    cursor.execute("CREATE SCHEMA acc_sales")
    cursor.execute("CREATE TABLE acc_sales.acc_orders (id int)")
    cursor.execute("GRANT USAGE ON SCHEMA acc_sales TO GROUP acc_analysts")
    cursor.execute("GRANT SELECT ON acc_sales.acc_orders TO GROUP acc_analysts")
    cursor.execute("GRANT SELECT ON acc_sales.acc_orders TO acc_bi")
    yield cursor
    cursor.execute("DROP SCHEMA IF EXISTS acc_sales CASCADE")
    cursor.execute("DROP GROUP IF EXISTS acc_analysts")
    cursor.execute("DROP USER IF EXISTS acc_bi")


def test_group_grant_acl_uses_redshift_group_prefix(group_grant):
    """Group grantees read back prefixed, as Redshift renders them, not bare."""
    for query in (SCHEMA_ACL_Q, TABLE_ACL_Q):
        group_grant.execute(query)
        acl = group_grant.fetchone()[0]
        assert "group acc_analysts=" in acl


def test_user_grant_acl_has_no_group_prefix(group_grant):
    """A user grantee stays bare, so only real groups pick up the prefix."""
    group_grant.execute(TABLE_ACL_Q)
    entries = group_grant.fetchone()[0].split(",")
    user = [e for e in entries if e.startswith("acc_bi=")]
    assert user and not any(e.startswith("group acc_bi=") for e in entries)


def test_acl_parses_as_a_group_not_a_user(group_grant):
    """Grant to a group, re-read, still a group, on a schema and on a table.

    Without the prefix both read back ("acc_analysts", "user"): a tool parsing the
    string sees the group holding nothing and re-plans the same GRANT forever.
    """
    for query in (SCHEMA_ACL_Q, TABLE_ACL_Q):
        group_grant.execute(query)
        holders = dict(
            parse_acl_holder(e) for e in group_grant.fetchone()[0].split(",")
        )
        assert holders["acc_analysts"] == "group"
    assert holders["acc_bi"] == "user"  # the user grant is on the table only


def test_compat_layer_reaches_the_postgres_database():
    """Tools that walk pg_database and reconnect per entry hit `postgres` too.

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


def test_connect_on_database_is_a_syntax_error(cursor):
    """As on Redshift Serverless: there is no CONNECT privilege to grant or revoke."""
    for stmt in (
        "GRANT CONNECT ON DATABASE oblako TO PUBLIC",
        "REVOKE CONNECT ON DATABASE oblako FROM PUBLIC",
    ):
        with pytest.raises(psycopg2.errors.SyntaxError, match='near "DATABASE"'):
            cursor.execute(stmt)


def test_a_user_drops_what_it_owns(cursor):
    """A user who isn't a superuser drops its own table, view and schema.

    The engine's drop event triggers (Iceberg, masking, ALTER/DROP grants) tidy
    their catalogs as their owner, so they don't refuse an ordinary DROP.
    """
    cursor.execute("DROP SCHEMA IF EXISTS acc_own CASCADE")
    cursor.execute("DROP USER IF EXISTS acc_owner")
    cursor.execute("CREATE USER acc_owner PASSWORD 'Owner_123'")
    cursor.execute("GRANT CREATE ON DATABASE oblako TO acc_owner")
    conn = psycopg2.connect(
        **{**RS_CONFIG, "user": "acc_owner", "password": "Owner_123"}
    )
    conn.autocommit = True
    try:
        with conn.cursor() as own:
            own.execute("CREATE SCHEMA acc_own")
            own.execute("CREATE TABLE acc_own.t (id int)")
            own.execute("CREATE VIEW acc_own.v AS SELECT id FROM acc_own.t")
            own.execute("DROP VIEW acc_own.v")
            own.execute("DROP TABLE acc_own.t")
            own.execute("DROP SCHEMA acc_own")
    finally:
        conn.close()
        cursor.execute("DROP SCHEMA IF EXISTS acc_own CASCADE")
        cursor.execute("REVOKE CREATE ON DATABASE oblako FROM acc_owner")
        cursor.execute("DROP USER acc_owner")


def test_a_late_binding_view_is_created_granted_and_read(cursor):
    """CREATE VIEW ... WITH NO SCHEMA BINDING works, as on Redshift Serverless."""
    cursor.execute("DROP SCHEMA IF EXISTS acc_lb CASCADE")
    cursor.execute("CREATE SCHEMA acc_lb")
    try:
        cursor.execute("CREATE TABLE acc_lb.events (id int)")
        cursor.execute("INSERT INTO acc_lb.events VALUES (1), (2)")
        cursor.execute(
            "CREATE VIEW acc_lb.v AS SELECT id FROM acc_lb.events WITH NO SCHEMA BINDING"
        )
        cursor.execute("GRANT SELECT ON acc_lb.v TO PUBLIC")
        cursor.execute("SELECT count(*) FROM acc_lb.v")
        assert cursor.fetchone() == (2,)
    finally:
        cursor.execute("DROP SCHEMA acc_lb CASCADE")


def test_pg_group_with_an_alias_or_no_schema_shows_only_groups(cursor):
    """As on Redshift: every spelling of pg_group lists only real groups."""
    for query in (
        "SELECT groname FROM pg_catalog.pg_group g",
        "SELECT groname FROM pg_group",
        "SELECT g.groname FROM pg_group AS g",
    ):
        cursor.execute(query)
        assert not any(
            name.startswith(("pg_", "sys:")) for (name,) in cursor.fetchall()
        )
