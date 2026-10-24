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
        "DROP USER IF EXISTS idt_bob",
        "DROP USER IF EXISTS idt_ann",
        "DROP GROUP IF EXISTS idt_analysts",
        "DROP ROLE IF EXISTS idt_writer FORCE",
        "DROP ROLE IF EXISTS idt_reader FORCE",
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


def test_a_regular_user_sees_only_its_own_role_grants(conn):
    """As on Redshift: svv_user_grants shows a non-superuser its own roles only,
    and svv_role_grants the roles it has or owns."""
    conn.execute("CREATE USER idt_bob PASSWORD 'Abcdef12'")
    conn.execute("GRANT ROLE idt_writer TO idt_bob")
    with psycopg.connect(
        autocommit=True, **{**RS, "user": "idt_alice", "password": "Abcdef12"}
    ) as alice:
        assert _rows(
            alice, "SELECT user_name, role_name FROM svv_user_grants ORDER BY 1, 2"
        ) == [("idt_alice", "idt_reader")]
        assert _rows(alice, "SELECT role_name FROM svv_role_grants") == []
        conn.execute("GRANT ROLE idt_writer TO idt_alice")
        assert _rows(
            alice, "SELECT role_name, granted_role_name FROM svv_role_grants"
        ) == [("idt_writer", "idt_reader")]
    assert ("idt_bob", "idt_writer") in _rows(
        conn, "SELECT user_name, role_name FROM svv_user_grants"
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


@pytest.fixture
def alter_drop(conn):
    """A role granted ALTER and DROP, a user in it, and objects to act on."""
    for stmt in [
        "DROP SCHEMA IF EXISTS idt_ad CASCADE",
        "DROP SCHEMA IF EXISTS idt_ad2 CASCADE",
        "DROP USER IF EXISTS idt_erin",
        "DROP ROLE IF EXISTS idt_ad_role FORCE",
        "CREATE SCHEMA idt_ad",
        "CREATE TABLE idt_ad.scratch (id int)",
        "CREATE VIEW idt_ad.v AS SELECT 1 AS x",
        "CREATE ROLE idt_ad_role",
        "CREATE USER idt_erin PASSWORD 'Abcdef12'",
        "GRANT ROLE idt_ad_role TO idt_erin",
        "GRANT USAGE, ALTER, DROP ON SCHEMA idt_ad TO ROLE idt_ad_role",
        "GRANT ALTER, DROP ON TABLE idt_ad.scratch TO ROLE idt_ad_role",
        "GRANT DROP ON TABLE idt_ad.v TO ROLE idt_ad_role",
    ]:
        conn.execute(stmt)
    yield conn
    for stmt in [
        "DROP SCHEMA IF EXISTS idt_ad CASCADE",
        "DROP SCHEMA IF EXISTS idt_ad2 CASCADE",
        "DROP USER IF EXISTS idt_erin",
        "DROP ROLE IF EXISTS idt_ad_role FORCE",
    ]:
        conn.execute(stmt)


def _as_erin():
    return psycopg.connect(
        autocommit=True, **{**RS, "user": "idt_erin", "password": "Abcdef12"}
    )


def test_alter_and_drop_privileges_read_back(alter_drop):
    assert _rows(
        alter_drop,
        "SELECT relation_name, privilege_type, identity_name, identity_type "
        "FROM svv_relation_privileges WHERE namespace_name = 'idt_ad' ORDER BY 1, 2",
    ) == [
        ("scratch", "ALTER", "idt_ad_role", "role"),
        ("scratch", "DROP", "idt_ad_role", "role"),
        ("v", "DROP", "idt_ad_role", "role"),
    ]
    assert _rows(
        alter_drop,
        "SELECT privilege_type FROM svv_schema_privileges "
        "WHERE namespace_name = 'idt_ad' ORDER BY 1",
    ) == [("ALTER",), ("DROP",), ("USAGE",)]
    alter_drop.execute("REVOKE DROP ON idt_ad.v FROM ROLE idt_ad_role")
    assert (
        _rows(
            alter_drop,
            "SELECT 1 FROM svv_relation_privileges WHERE relation_name = 'v' "
            "AND namespace_name = 'idt_ad'",
        )
        == []
    )


def test_alter_and_drop_privileges_are_enforced(alter_drop):
    """As on Redshift Serverless: ALTER and DROP let a user who isn't the owner act."""
    with _as_erin() as erin:
        erin.execute("ALTER TABLE idt_ad.scratch ADD COLUMN extra int")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            erin.execute("SELECT count(*) FROM idt_ad.scratch")  # ALTER isn't SELECT
        erin.execute("DROP VIEW idt_ad.v")
        erin.execute("DROP TABLE idt_ad.scratch")
        erin.execute("ALTER SCHEMA idt_ad RENAME TO idt_ad2")
    # the dropped objects take their grants with them
    assert (
        _rows(
            alter_drop,
            "SELECT 1 FROM svv_relation_privileges WHERE identity_name = 'idt_ad_role'",
        )
        == []
    )


def test_without_the_privilege_only_the_owner_acts_or_grants(alter_drop):
    alter_drop.execute("CREATE TABLE idt_ad.kept (id int)")
    with _as_erin() as erin:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            erin.execute("DROP TABLE idt_ad.kept")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            erin.execute("GRANT ALTER ON idt_ad.kept TO idt_erin")


def test_system_roles(conn):
    """Redshift's sys:* roles exist as roles; sys:operator reads every role grant,
    and sys:dba drops a table it doesn't own."""
    assert _rows(
        conn, "SELECT role_name FROM svv_roles WHERE role_name LIKE 'sys:%' ORDER BY 1"
    ) == [
        ("sys:dba",),
        ("sys:monitor",),
        ("sys:operator",),
        ("sys:secadmin",),
        ("sys:superuser",),
    ]
    conn.execute("CREATE USER idt_bob PASSWORD 'Abcdef12'")
    conn.execute('GRANT ROLE "sys:operator" TO idt_bob')
    with psycopg.connect(
        autocommit=True, **{**RS, "user": "idt_bob", "password": "Abcdef12"}
    ) as bob:
        assert ("idt_alice", "idt_reader") in _rows(
            bob, "SELECT user_name, role_name FROM svv_user_grants"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            bob.execute("DROP TABLE idt.events")
    conn.execute('GRANT ROLE "sys:dba" TO idt_bob')
    conn.execute("GRANT USAGE ON SCHEMA idt TO idt_bob")
    with psycopg.connect(
        autocommit=True, **{**RS, "user": "idt_bob", "password": "Abcdef12"}
    ) as bob:
        bob.execute("DROP TABLE idt.events")


def test_drop_role_restrict_and_force(conn):
    """As on Redshift: a granted role, or one holding another, drops only with FORCE."""
    with pytest.raises(psycopg.errors.DependentObjectsStillExist, match="on a user"):
        conn.execute("DROP ROLE idt_reader")
    with pytest.raises(psycopg.errors.DependentObjectsStillExist, match="another role"):
        conn.execute("DROP ROLE idt_writer RESTRICT")
    conn.execute("DROP ROLE idt_writer FORCE")
    conn.execute("REVOKE ROLE idt_reader FROM idt_alice")
    conn.execute("REVOKE SELECT ON idt.events FROM ROLE idt_reader")
    conn.execute("REVOKE USAGE ON SCHEMA idt FROM ROLE idt_reader")
    conn.execute(
        "ALTER DEFAULT PRIVILEGES FOR USER oblako IN SCHEMA idt "
        "REVOKE SELECT ON TABLES FROM ROLE idt_reader"
    )
    conn.execute("DROP ROLE idt_reader")
    assert _rows(conn, "SELECT 1 FROM svv_roles WHERE role_name LIKE 'idt%'") == []


def test_default_privileges_in_a_schema_need_create_there(conn):
    """As on Redshift: FOR USER x IN SCHEMA s needs x to be able to CREATE in s,
    whoever runs it; without IN SCHEMA nothing is checked."""
    conn.execute("CREATE USER idt_bob PASSWORD 'Abcdef12'")
    conn.execute("CREATE ROLE idt_creator")

    def adp(action: str = "GRANT SELECT ON TABLES TO ROLE idt_reader") -> None:
        conn.execute(
            f"ALTER DEFAULT PRIVILEGES FOR USER idt_bob IN SCHEMA idt {action}"
        )

    def refused(action: str = "GRANT SELECT ON TABLES TO ROLE idt_reader") -> None:
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege,
            match="permission denied for schema idt",
        ):
            adp(action)

    try:
        refused()
        conn.execute("GRANT USAGE ON SCHEMA idt TO idt_bob")
        refused()
        refused("REVOKE SELECT ON TABLES FROM ROLE idt_reader")
        conn.execute(
            "ALTER DEFAULT PRIVILEGES FOR USER idt_bob "
            "GRANT SELECT ON TABLES TO ROLE idt_reader"
        )
        conn.execute(
            "ALTER DEFAULT PRIVILEGES FOR USER idt_bob "
            "REVOKE SELECT ON TABLES FROM ROLE idt_reader"
        )
        for grant, revoke in [
            ("GRANT CREATE ON SCHEMA idt TO idt_bob", None),
            (
                "GRANT CREATE ON SCHEMA idt TO GROUP idt_analysts",
                "ALTER GROUP idt_analysts ADD USER idt_bob",
            ),
            (
                "GRANT CREATE ON SCHEMA idt TO ROLE idt_creator",
                "GRANT ROLE idt_creator TO idt_bob",
            ),
            (
                "GRANT CREATE ON SCHEMA idt TO ROLE idt_creator",
                "GRANT ROLE idt_creator TO ROLE idt_reader; GRANT ROLE idt_reader TO idt_bob",
            ),
            ("GRANT CREATE ON SCHEMA idt TO PUBLIC", None),
            ("ALTER SCHEMA idt OWNER TO idt_bob", None),
            ("ALTER USER idt_bob CREATEUSER", None),
        ]:
            for stmt in [grant, *(revoke.split("; ") if revoke else [])]:
                conn.execute(stmt)
            adp()
            adp("REVOKE SELECT ON TABLES FROM ROLE idt_reader")
            for stmt in [
                "ALTER USER idt_bob NOCREATEUSER",
                "ALTER SCHEMA idt OWNER TO oblako",
                "REVOKE CREATE ON SCHEMA idt FROM PUBLIC",
                "REVOKE CREATE ON SCHEMA idt FROM idt_bob",
                "REVOKE CREATE ON SCHEMA idt FROM GROUP idt_analysts",
                "REVOKE CREATE ON SCHEMA idt FROM ROLE idt_creator",
                "REVOKE ROLE idt_creator FROM ROLE idt_reader",
                "REVOKE ROLE idt_creator FROM idt_bob",
                "REVOKE ROLE idt_reader FROM idt_bob",
            ]:
                conn.execute(stmt)
            refused()
    finally:
        conn.execute("DROP ROLE IF EXISTS idt_creator FORCE")


def test_access_system_table_through_a_role_sees_everything(conn):
    """As on Redshift Serverless: ACCESS SYSTEM TABLE, held through roles, shows every
    row of the SVV views and answers membership for other users, without data access."""
    conn.execute("CREATE USER idt_bob PASSWORD 'Abcdef12'")
    conn.execute("CREATE ROLE idt_ast")
    conn.execute("CREATE ROLE idt_ast_outer")
    try:
        conn.execute("GRANT ROLE idt_ast TO ROLE idt_ast_outer")
        conn.execute("GRANT ROLE idt_ast_outer TO idt_bob")
        with psycopg.connect(
            autocommit=True, **{**RS, "user": "idt_bob", "password": "Abcdef12"}
        ) as bob:
            assert ("idt_alice", "idt_reader") not in _rows(
                bob, "SELECT user_name, role_name FROM svv_user_grants"
            )
            with pytest.raises(
                psycopg.errors.InsufficientPrivilege, match="ACCESS SYSTEM"
            ):
                bob.execute("SELECT user_is_member_of('idt_alice', 'idt_reader')")
            conn.execute("GRANT ACCESS SYSTEM TABLE TO ROLE idt_ast")
            assert ("idt_alice", "idt_reader") in _rows(
                bob, "SELECT user_name, role_name FROM svv_user_grants"
            )
            assert _rows(
                bob, "SELECT user_is_member_of('idt_alice', 'idt_reader')"
            ) == [(True,)]
            assert _rows(
                bob, "SELECT role_is_member_of('idt_writer', 'idt_reader')"
            ) == [(True,)]
            assert ("ACCESS SYSTEM TABLE", "idt_ast", "role") in _rows(
                bob,
                "SELECT system_privilege, identity_name, identity_type "
                "FROM svv_system_privileges",
            )
            assert _rows(
                bob, "SELECT usesuper FROM pg_user WHERE usename = 'idt_bob'"
            ) == [(False,)]
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                bob.execute("SELECT * FROM idt.events")
            conn.execute("REVOKE ACCESS SYSTEM TABLE FROM ROLE idt_ast")
            assert ("idt_alice", "idt_reader") not in _rows(
                bob, "SELECT user_name, role_name FROM svv_user_grants"
            )
    finally:
        conn.execute("DROP USER IF EXISTS idt_bob")
        conn.execute("DROP ROLE IF EXISTS idt_ast_outer FORCE")
        conn.execute("DROP ROLE IF EXISTS idt_ast FORCE")


def test_system_permissions_on_users_and_roles(conn):
    """As on Redshift Serverless: CREATE USER, ALTER USER, DROP USER and CREATE ROLE
    are granted to and revoked from a role, and listed in svv_system_privileges."""
    privileges = ["ALTER USER", "CREATE ROLE", "CREATE USER", "DROP USER"]
    listed = (
        "SELECT system_privilege FROM svv_system_privileges "
        "WHERE identity_name = 'idt_reader' AND identity_type = 'role' ORDER BY 1"
    )
    for privilege in privileges:
        conn.execute(f"GRANT {privilege} TO ROLE idt_reader")
    assert _rows(conn, listed) == [(p,) for p in privileges]
    conn.execute("REVOKE CREATE USER, DROP USER FROM ROLE idt_reader")
    assert _rows(conn, listed) == [("ALTER USER",), ("CREATE ROLE",)]
    conn.execute("REVOKE ALTER USER, CREATE ROLE FROM ROLE idt_reader")
    assert _rows(conn, listed) == []


def test_a_superuser_needs_a_password(conn):
    """As on Redshift Serverless: CREATEUSER and PASSWORD DISABLE are refused together."""
    with pytest.raises(
        psycopg.errors.SyntaxError,
        match="CREATEUSER and PASSWORD DISABLE are incompatible",
    ):
        conn.execute("CREATE USER idt_bob CREATEUSER PASSWORD DISABLE")
    assert _rows(conn, "SELECT 1 FROM pg_user WHERE usename = 'idt_bob'") == []


def test_user_info_views_hide_every_password(conn):
    """As on Redshift Serverless: '********' for every user, a disabled password included."""
    conn.execute("CREATE USER idt_ann PASSWORD DISABLE")
    assert (
        _rows(
            conn,
            "SELECT passwd, usesuper, useconnlimit FROM pg_user_info"
            " WHERE usename IN ('idt_alice', 'idt_ann') ORDER BY usename",
        )
        == [("********", False, "UNLIMITED")] * 2
    )
    assert _rows(
        conn,
        "SELECT superuser, connection_limit, syslog_access FROM svv_user_info"
        " WHERE user_name = 'idt_ann'",
    ) == [(False, "UNLIMITED", "RESTRICTED")]
    names = [r[0] for r in _rows(conn, "SELECT user_name FROM svv_user_info")]
    assert "idt_reader" not in names and "idt_analysts" not in names
    with psycopg.connect(
        autocommit=True, **{**RS, "user": "idt_alice", "password": "Abcdef12"}
    ) as alice:
        for view in ("pg_user_info", "svv_user_info"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                alice.execute(f"SELECT 1 FROM {view}")


def test_alter_user_cannot_leave_a_superuser_without_a_password(conn):
    """As on Redshift Serverless, judged on the user's state after the statement."""

    def superuser(name):
        return _rows(conn, f"SELECT usesuper FROM pg_user WHERE usename = '{name}'")

    refused = "Superusers cannot have disabled passwords"
    conn.execute("CREATE USER idt_ann PASSWORD DISABLE")
    with pytest.raises(psycopg.errors.SyntaxError, match=refused):
        conn.execute("ALTER USER idt_ann CREATEUSER")
    assert superuser("idt_ann") == [(False,)]
    conn.execute("ALTER USER idt_ann CREATEUSER PASSWORD 'Probe-pw-1'")
    assert superuser("idt_ann") == [(True,)]
    with pytest.raises(psycopg.errors.SyntaxError, match=refused):
        conn.execute("ALTER USER idt_ann PASSWORD DISABLE")
    assert superuser("idt_ann") == [(True,)]
    conn.execute("ALTER USER idt_ann NOCREATEUSER PASSWORD DISABLE")
    assert superuser("idt_ann") == [(False,)]
