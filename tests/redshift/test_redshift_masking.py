"""Integration tests: Redshift dynamic data masking (DDM) policies, the catalog.

Requires the engine (docker compose up redshift). redshift-local keeps masking
policies and their attachments and answers svv_masking_policy and
svv_attached_masking_policy with Redshift's columns and JSON formats; it enforces
the rules Redshift Serverless enforces (checked 2026-10-07): priorities, clashes,
DROP while attached, ALTER keeping the output type, superusers only. A query
then reads each masked column as the highest-priority attachment for the user
gives it.

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


# ---------------------------------------------------------------------------
# Query time: what each user reads
# ---------------------------------------------------------------------------
READERS = ("ddm_plain", "ddm_sup", "ddm_both")


def _drop_readers(c) -> None:
    for stmt in [
        "DROP SCHEMA IF EXISTS ddm CASCADE",
        *(f"DROP MASKING POLICY {p}" for p in ("ddm_redact", "ddm_domain", "ddm_raw")),
        *(f"DROP USER IF EXISTS {u}" for u in READERS),
        "DROP ROLE IF EXISTS ddm_support",
        "DROP ROLE IF EXISTS ddm_pii",
    ]:
        try:
            c.execute(stmt)
        except psycopg.Error:
            pass  # not there yet


@pytest.fixture
def readers():
    with psycopg.connect(autocommit=True, **RS) as c:
        _drop_readers(c)
        for stmt in [
            "CREATE SCHEMA ddm",
            "CREATE TABLE ddm.customers (id int, email varchar(64))",
            "INSERT INTO ddm.customers VALUES (1, 'ann@example.com'), (2, 'bob@example.com')",
            "CREATE ROLE ddm_support",
            "CREATE ROLE ddm_pii",
            *(f"CREATE USER {u} PASSWORD 'Abcdef12'" for u in READERS),
            "GRANT ROLE ddm_support TO ddm_sup",
            "GRANT ROLE ddm_support TO ddm_both",
            "GRANT ROLE ddm_pii TO ddm_both",
            "GRANT USAGE ON SCHEMA ddm TO PUBLIC",
            "GRANT SELECT ON ddm.customers TO PUBLIC",
            "CREATE MASKING POLICY ddm_redact WITH (email varchar(64)) "
            "USING ('***'::varchar(64))",
            "CREATE MASKING POLICY ddm_domain WITH (email varchar(64)) "
            "USING (regexp_replace(email, '^[^@]+', '***'))",
            "CREATE MASKING POLICY ddm_raw WITH (email varchar(64)) USING (email)",
            "ATTACH MASKING POLICY ddm_redact ON ddm.customers(email) TO PUBLIC PRIORITY 10",
            "ATTACH MASKING POLICY ddm_domain ON ddm.customers(email) "
            "TO ROLE ddm_support PRIORITY 20",
            "ATTACH MASKING POLICY ddm_raw ON ddm.customers(email) TO ROLE ddm_pii PRIORITY 1000",
        ]:
            c.execute(stmt)
        yield c
        _drop_readers(c)


def _read(user: str, sql: str, params=None) -> list[tuple]:
    with psycopg.connect(**{**RS, "user": user, "password": "Abcdef12"}) as c:
        return c.execute(sql, params).fetchall()


def test_each_user_reads_what_the_highest_priority_gives_them(readers):
    query = "SELECT id, email FROM ddm.customers ORDER BY id"
    # straight after the ATTACHes: nothing cached from before them
    assert _read("ddm_plain", query) == [(1, "***"), (2, "***")]
    assert _read("ddm_sup", query) == [(1, "***@example.com"), (2, "***@example.com")]
    assert _read("ddm_both", query) == [(1, "ann@example.com"), (2, "bob@example.com")]


def test_masking_reaches_every_way_of_reading_the_table(readers):
    for sql, params in [
        ("SELECT c.email FROM ddm.customers c WHERE c.id = 1", None),
        ("SELECT customers.email FROM ddm.customers WHERE id = 1", None),
        ("SELECT email FROM ddm.customers WHERE id = %s", (1,)),  # extended protocol
        (
            "SELECT email FROM (SELECT email, id FROM ddm.customers) s WHERE id = 1",
            None,
        ),
        (
            "WITH x AS (SELECT * FROM ddm.customers) SELECT email FROM x WHERE id = 1",
            None,
        ),
        (
            "SELECT o.email FROM (SELECT 1 AS id) i JOIN ddm.customers o ON o.id = i.id",
            None,
        ),
        (
            "SELECT email FROM (SELECT 1 AS id) i, ddm.customers WHERE customers.id = 1",
            None,
        ),
    ]:
        assert _read("ddm_plain", sql, params) == [("***",)], sql


def test_the_column_keeps_its_type(readers):
    with psycopg.connect(**{**RS, "user": "ddm_sup", "password": "Abcdef12"}) as c:
        cur = c.execute("SELECT email FROM ddm.customers LIMIT 1")
        assert cur.description[0].type_code == 1043  # varchar


def test_alter_and_detach_take_effect_at_once(readers):
    query = "SELECT email FROM ddm.customers WHERE id = 1"
    readers.execute("ALTER MASKING POLICY ddm_redact USING ('#####'::varchar(64))")
    assert _read("ddm_plain", query) == [("#####",)]
    readers.execute(
        "DETACH MASKING POLICY ddm_redact ON ddm.customers(email) FROM PUBLIC"
    )
    assert _read("ddm_plain", query) == [("ann@example.com",)]


def test_the_table_stays_a_table(readers):
    readers.execute("INSERT INTO ddm.customers VALUES (3, 'cy@example.com')")
    readers.execute("UPDATE ddm.customers SET email = 'cy@example.org' WHERE id = 3")
    readers.execute("ALTER TABLE ddm.customers ADD COLUMN tier int")
    rows = _read("ddm_plain", "SELECT * FROM ddm.customers WHERE id = 3")
    assert rows == [(3, "***", None)]
    readers.execute("DROP TABLE ddm.customers")  # nothing depends on it
    assert readers.execute(
        "SELECT count(*) FROM svv_attached_masking_policy WHERE schema_name = 'ddm'"
    ).fetchone() == (0,)
