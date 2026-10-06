"""Integration tests: Redshift users, groups and roles, and the SVV privilege views.

Requires the engine (docker compose up redshift). Redshift keeps users, groups and
(RBAC) roles apart and reports privileges through SVV views; access tools such as
pgsesame read those instead of ACL strings. These tests create one of each
identity, grant through every Redshift form, and read the grants back with the
columns and values AWS documents.

Override the port with OBLAKO_TEST_RS_PORT to run against an isolated stack.
"""

import os

import pytest

psycopg = pytest.importorskip("psycopg")

RS_PORT = int(os.environ.get("OBLAKO_TEST_RS_PORT", "5439"))
RS = dict(
    host="localhost", port=RS_PORT, user="oblako", password="oblako", dbname="oblako"
)
SETUP = [
    "CREATE USER idt_alice PASSWORD 'Abcdef12'",
    "CREATE GROUP idt_analysts",
    "ALTER GROUP idt_analysts ADD USER idt_alice",
    "CREATE ROLE idt_reader",
    "CREATE ROLE idt_writer",
    "GRANT ROLE idt_reader TO idt_alice",
    "GRANT ROLE idt_reader TO ROLE idt_writer",
    "CREATE SCHEMA idt",
    "CREATE TABLE idt.events (id int)",
    "GRANT USAGE ON SCHEMA idt TO ROLE idt_reader",
    "GRANT SELECT ON idt.events TO ROLE idt_reader",
    "GRANT SELECT, INSERT ON idt.events TO GROUP idt_analysts",
    "GRANT UPDATE ON idt.events TO idt_alice",
    "ALTER DEFAULT PRIVILEGES FOR USER oblako IN SCHEMA idt "
    "GRANT SELECT ON TABLES TO ROLE idt_reader",
]


def _engine_up() -> bool:
    try:
        psycopg.connect(connect_timeout=3, **RS).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _engine_up(), reason="redshift engine not running")


def _drop(c) -> None:
    for stmt in [
        "ALTER DEFAULT PRIVILEGES FOR USER oblako IN SCHEMA idt "
        "REVOKE SELECT ON TABLES FROM ROLE idt_reader",
        "DROP SCHEMA IF EXISTS idt CASCADE",
        "DROP USER IF EXISTS idt_alice",
        "DROP GROUP IF EXISTS idt_analysts",
        "DROP ROLE IF EXISTS idt_writer",
        "DROP ROLE IF EXISTS idt_reader",
    ]:
        try:
            c.execute(stmt)
        except psycopg.Error:
            pass  # not there yet


@pytest.fixture
def conn():
    with psycopg.connect(autocommit=True, **RS) as c:
        _drop(c)
        for stmt in SETUP:
            c.execute(stmt)
        yield c
        _drop(c)


def _rows(c, sql: str) -> list[tuple]:
    return c.execute(sql).fetchall()


def test_roles_are_not_groups(conn):
    assert _rows(
        conn,
        "SELECT role_name, role_owner FROM svv_roles WHERE role_name LIKE 'idt%' "
        "ORDER BY 1",
    ) == [("idt_reader", "oblako"), ("idt_writer", "oblako")]
    groups = _rows(
        conn, "SELECT groname FROM pg_catalog.pg_group WHERE groname LIKE 'idt%'"
    )
    assert groups == [("idt_analysts",)]


def test_role_grants(conn):
    assert _rows(
        conn,
        "SELECT user_name, role_name, admin_option FROM svv_user_grants "
        "WHERE user_name = 'idt_alice'",
    ) == [("idt_alice", "idt_reader", False)]
    assert _rows(
        conn,
        "SELECT role_name, granted_role_name FROM svv_role_grants "
        "WHERE role_name LIKE 'idt%'",
    ) == [("idt_writer", "idt_reader")]
    conn.execute("REVOKE ROLE idt_reader FROM idt_alice")
    assert (
        _rows(conn, "SELECT 1 FROM svv_user_grants WHERE user_name = 'idt_alice'") == []
    )


def test_relation_and_schema_privileges(conn):
    assert _rows(
        conn,
        "SELECT relation_name, privilege_type, identity_name, identity_type, admin_option "
        "FROM svv_relation_privileges WHERE namespace_name = 'idt' ORDER BY 3, 2",
    ) == [
        ("events", "UPDATE", "idt_alice", "user", False),
        ("events", "INSERT", "idt_analysts", "group", False),
        ("events", "SELECT", "idt_analysts", "group", False),
        ("events", "SELECT", "idt_reader", "role", False),
    ]
    assert _rows(
        conn,
        "SELECT privilege_type, identity_name, identity_type, privilege_scope "
        "FROM svv_schema_privileges WHERE namespace_name = 'idt'",
    ) == [("USAGE", "idt_reader", "role", "SCHEMA")]


def test_default_privileges(conn):
    assert _rows(
        conn,
        "SELECT schema_name, object_type, owner_name, owner_type, privilege_type, "
        "grantee_name, grantee_type FROM svv_default_privileges "
        "WHERE schema_name = 'idt'",
    ) == [("idt", "RELATION", "oblako", "user", "SELECT", "idt_reader", "role")]


def test_acl_strings_prefix_groups_and_leave_out_roles(conn):
    (acl,) = conn.execute(
        "SELECT array_to_string(relacl, ',') FROM pg_class "
        "WHERE oid = 'idt.events'::regclass"
    ).fetchone()
    entries = acl.split(",")
    assert "group idt_analysts=ar/oblako" in entries
    assert "idt_alice=w/oblako" in entries
    # a grant to a role shows only in the SVV views, as on Redshift
    assert not any(e.startswith("idt_reader=") for e in entries)


def test_views_have_redshifts_columns(conn):
    expected = {
        "svv_roles": ["role_id", "role_name", "role_owner", "external_id"],
        "svv_user_grants": [
            "user_id",
            "user_name",
            "role_id",
            "role_name",
            "admin_option",
        ],
        "svv_role_grants": [
            "role_id",
            "role_name",
            "granted_role_id",
            "granted_role_name",
        ],
        "svv_relation_privileges": [
            "namespace_name",
            "relation_name",
            "privilege_type",
            "identity_id",
            "identity_name",
            "identity_type",
            "admin_option",
        ],
        "svv_schema_privileges": [
            "namespace_name",
            "privilege_type",
            "identity_id",
            "identity_name",
            "identity_type",
            "admin_option",
            "privilege_scope",
        ],
        "svv_database_privileges": [
            "database_name",
            "privilege_type",
            "identity_id",
            "identity_name",
            "identity_type",
            "admin_option",
            "privilege_scope",
        ],
        "svv_function_privileges": [
            "namespace_name",
            "function_name",
            "argument_types",
            "privilege_type",
            "identity_id",
            "identity_name",
            "identity_type",
            "admin_option",
        ],
        "svv_default_privileges": [
            "schema_name",
            "object_type",
            "owner_id",
            "owner_name",
            "owner_type",
            "privilege_type",
            "grantee_id",
            "grantee_name",
            "grantee_type",
            "admin_option",
        ],
    }
    for view, columns in expected.items():
        cur = conn.execute(f"SELECT * FROM {view} LIMIT 0")
        assert [d.name for d in cur.description] == columns, view
