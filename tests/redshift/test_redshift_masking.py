"""Integration tests: Redshift dynamic data masking (DDM) policies, the catalog.

Requires the engine (docker compose up redshift). redshift-local keeps masking
policies and their attachments and answers svv_masking_policy and
svv_attached_masking_policy with Redshift's columns and JSON formats; it enforces
the rules Redshift Serverless enforces (checked 2026-10-07): priorities, clashes,
DROP while attached, ALTER keeping the output type, superusers only. Queries are
not masked yet.

Override the port with OBLAKO_TEST_RS_PORT to run against an isolated stack.
"""

import json
import os

import pytest

psycopg = pytest.importorskip("psycopg")

RS_PORT = int(os.environ.get("OBLAKO_TEST_RS_PORT", "5439"))
RS = dict(
    host="localhost", port=RS_PORT, user="oblako", password="oblako", dbname="oblako"
)
POLICIES = ("ddm_full", "ddm_partial")


def _engine_up() -> bool:
    try:
        psycopg.connect(connect_timeout=3, **RS).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _engine_up(), reason="redshift engine not running")


def _drop(c) -> None:
    # the schema first: its table takes the attachments, then the policies can go
    for stmt in [
        "DROP SCHEMA IF EXISTS ddm CASCADE",
        *(f"DROP MASKING POLICY {p}" for p in POLICIES),
        "DROP USER IF EXISTS ddm_bob",
        "DROP ROLE IF EXISTS ddm_analyst",
    ]:
        try:
            c.execute(stmt)
        except psycopg.Error:
            pass  # not there yet


@pytest.fixture
def conn():
    with psycopg.connect(autocommit=True, **RS) as c:
        _drop(c)
        for stmt in [
            "CREATE SCHEMA ddm",
            "CREATE TABLE ddm.users (id int, email varchar(64))",
            "CREATE ROLE ddm_analyst",
            "CREATE USER ddm_bob PASSWORD 'Abcdef12'",
            "CREATE MASKING POLICY ddm_full WITH (email varchar(256)) "
            "USING ('***'::varchar(256))",
            "CREATE MASKING POLICY ddm_partial WITH (email varchar(256)) "
            "USING (regexp_replace(email, '^[^@]+', '***'))",
        ]:
            c.execute(stmt)
        yield c
        _drop(c)


def _attached(c) -> list[tuple]:
    return c.execute(
        "SELECT policy_name, schema_name, table_name, table_type, grantee, "
        "grantee_type, priority, input_columns, output_columns "
        "FROM svv_attached_masking_policy WHERE schema_name = 'ddm' "
        "ORDER BY policy_name, grantee, priority"
    ).fetchall()


def test_policies_read_back_in_redshifts_json(conn):
    rows = conn.execute(
        "SELECT policy_database, policy_name, input_columns, policy_expression, "
        "policy_modified_by FROM svv_masking_policy "
        "WHERE policy_name IN ('ddm_full', 'ddm_partial') ORDER BY policy_name"
    ).fetchall()
    assert [r[1] for r in rows] == ["ddm_full", "ddm_partial"]
    database, _, inputs, expression, by = rows[1]
    assert database == "oblako" and by == "oblako"
    assert json.loads(inputs) == [
        {"colname": "email", "type": "character varying(256)"}
    ]
    assert json.loads(expression) == [
        {"expr": "regexp_replace(email, '^[^@]+', '***')", "type": "text"}
    ]
    with pytest.raises(psycopg.Error, match="already exists"):
        conn.execute("CREATE MASKING POLICY ddm_full WITH (a int) USING (a)")
    conn.execute("CREATE MASKING POLICY IF NOT EXISTS ddm_full WITH (a int) USING (a)")


def test_attach_to_roles_users_and_public_by_priority(conn):
    for stmt in [
        "ATTACH MASKING POLICY ddm_full ON ddm.users(email) TO ROLE ddm_analyst PRIORITY 10",
        "ATTACH MASKING POLICY ddm_full ON ddm.users(email) TO PUBLIC PRIORITY 10",
        "ATTACH MASKING POLICY ddm_partial ON ddm.users(email) TO ddm_bob PRIORITY 20",
        "ATTACH MASKING POLICY ddm_partial ON ddm.users(email) TO ddm_bob PRIORITY 30",
    ]:
        conn.execute(stmt)
    assert _attached(conn) == [
        (
            "ddm_full",
            "ddm",
            "users",
            "table",
            "ddm_analyst",
            "role",
            10,
            '["email"]',
            '["email"]',
        ),
        (
            "ddm_full",
            "ddm",
            "users",
            "table",
            "public",
            "public",
            10,
            '["email"]',
            '["email"]',
        ),
        (
            "ddm_partial",
            "ddm",
            "users",
            "table",
            "ddm_bob",
            "user",
            20,
            '["email"]',
            '["email"]',
        ),
        (
            "ddm_partial",
            "ddm",
            "users",
            "table",
            "ddm_bob",
            "user",
            30,
            '["email"]',
            '["email"]',
        ),
    ]
    # a different policy can't share a priority on the column
    with pytest.raises(psycopg.Error, match="same priority"):
        conn.execute(
            "ATTACH MASKING POLICY ddm_partial ON ddm.users(email) TO ddm_bob PRIORITY 10"
        )
    # one DETACH removes every priority of that grantee
    conn.execute("DETACH MASKING POLICY ddm_partial ON ddm.users(email) FROM ddm_bob")
    assert [r[0] for r in _attached(conn)] == ["ddm_full", "ddm_full"]


def test_forms_redshift_refuses(conn):
    with pytest.raises(psycopg.errors.SyntaxError):
        conn.execute("ATTACH MASKING POLICY ddm_full ON ddm.users(email) TO GROUP g")
    with pytest.raises(psycopg.Error, match="does not exist"):
        conn.execute("ATTACH MASKING POLICY ddm_full ON ddm.users(nope) TO PUBLIC")
    conn.execute("ATTACH MASKING POLICY ddm_full ON ddm.users(email) TO PUBLIC")
    with pytest.raises(psycopg.Error, match="depend on it"):
        conn.execute("DROP MASKING POLICY ddm_full")


def test_alter_changes_the_expression_not_its_type(conn):
    conn.execute("ALTER MASKING POLICY ddm_full USING (email)")  # varchar(256) too
    # compared exactly, as Redshift does: a length or text is another type
    for expression in ("42", "'#'::varchar(10)", "upper(email)"):
        with pytest.raises(psycopg.Error, match="different types"):
            conn.execute(f"ALTER MASKING POLICY ddm_full USING ({expression})")
    (expression,) = conn.execute(
        "SELECT policy_expression FROM svv_masking_policy WHERE policy_name = 'ddm_full'"
    ).fetchone()
    assert json.loads(expression)[0]["expr"] == "email"


def test_a_dropped_table_takes_its_attachments(conn):
    conn.execute("ATTACH MASKING POLICY ddm_full ON ddm.users(email) TO PUBLIC")
    conn.execute("DROP TABLE ddm.users")
    assert _attached(conn) == []
    conn.execute("DROP MASKING POLICY ddm_full")  # nothing depends on it now


def test_only_superusers_see_and_manage_policies(conn):
    conn.execute("ATTACH MASKING POLICY ddm_full ON ddm.users(email) TO PUBLIC")
    with psycopg.connect(
        **{**RS, "user": "ddm_bob", "password": "Abcdef12"}, autocommit=True
    ) as bob:
        for view in ("svv_masking_policy", "svv_attached_masking_policy"):
            assert bob.execute(f"SELECT count(*) FROM {view}").fetchone() == (0,)
        with pytest.raises(psycopg.Error, match="permission denied"):
            bob.execute("CREATE MASKING POLICY ddm_bobs WITH (a int) USING (a)")


def test_column_privileges(conn):
    conn.execute("GRANT SELECT (id, email) ON ddm.users TO ddm_bob")
    conn.execute("GRANT UPDATE (email) ON ddm.users TO ROLE ddm_analyst")
    rows = conn.execute(
        "SELECT relation_name, column_name, privilege_type, identity_name, identity_type "
        "FROM svv_column_privileges WHERE namespace_name = 'ddm' ORDER BY 2, 3, 4"
    ).fetchall()
    assert rows == [
        ("users", "email", "SELECT", "ddm_bob", "user"),
        ("users", "email", "UPDATE", "ddm_analyst", "role"),
        ("users", "id", "SELECT", "ddm_bob", "user"),
    ]


def test_an_expression_of_ambiguous_type_is_refused(conn):
    with pytest.raises(psycopg.errors.FeatureNotSupported, match="ambiguous type"):
        conn.execute("CREATE MASKING POLICY ddm_bare WITH (a varchar(9)) USING ('***')")
    with pytest.raises(psycopg.errors.FeatureNotSupported, match="ambiguous type"):
        conn.execute("ALTER MASKING POLICY ddm_full USING ('#')")
    # a literal in a CASE takes its type from the other branch
    conn.execute(
        "CREATE MASKING POLICY ddm_bare WITH (a varchar(9)) "
        "USING (CASE WHEN a LIKE '%@%' THEN '***' ELSE a END)"
    )
    conn.execute("DROP MASKING POLICY ddm_bare")
