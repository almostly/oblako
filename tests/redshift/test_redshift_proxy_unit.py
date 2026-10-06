"""Unit tests for the Redshift-compat proxy's SQL rewriter (no services)."""

import importlib.util
import pathlib
import struct

import pytest

_PATH = (
    pathlib.Path(__file__).parents[2] / "oblako/images/redshift/proxy/redshift_proxy.py"
)
_spec = importlib.util.spec_from_file_location("redshift_proxy", _PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
rewrite_sql = _mod.rewrite_sql
extract_distribution = _mod.extract_distribution


def _norm(s: str) -> str:
    return " ".join(s.split())


def test_strips_diststyle():
    assert _norm(rewrite_sql("CREATE TABLE t (id int) DISTSTYLE AUTO")) == (
        "CREATE TABLE t (id int)"
    )


def test_strips_only_the_create_table_statements():
    sql = (
        "CREATE TABLE t (id int) DISTKEY(id); "
        "SELECT a.attname AS distkey, 'x;y' AS encode FROM pg_attribute a"
    )
    first, rest = rewrite_sql(sql).split(";", 1)
    assert "distkey" not in first.lower()
    assert rest == " SELECT a.attname AS distkey, 'x;y' AS encode FROM pg_attribute a"


def test_strips_all_physical_ddl():
    out = rewrite_sql(
        "CREATE TABLE t (id int ENCODE az64, n varchar(5) ENCODE lzo) "
        "DISTSTYLE KEY DISTKEY (id) COMPOUND SORTKEY (id, n)"
    ).upper()
    for kw in ("ENCODE", "DISTSTYLE", "DISTKEY", "SORTKEY"):
        assert kw not in out
    assert "ID INT" in out and "N VARCHAR(5)" in out


def test_backup_clause_stripped():
    assert (
        "BACKUP"
        not in rewrite_sql("CREATE TABLE t (a int) DISTSTYLE ALL BACKUP NO").upper()
    )


def test_temp_and_unlogged_tables_handled():
    # dbt and others create temp tables; those must be stripped too
    assert (
        "DISTSTYLE"
        not in rewrite_sql("CREATE TEMP TABLE t (a int) DISTSTYLE EVEN").upper()
    )
    assert (
        "SORTKEY"
        not in rewrite_sql("CREATE TEMPORARY TABLE t (a int) SORTKEY (a)").upper()
    )
    assert (
        "DISTKEY"
        not in rewrite_sql("CREATE UNLOGGED TABLE t (a int) DISTKEY (a)").upper()
    )


def test_non_ddl_untouched():
    # not a CREATE TABLE -> leave it alone (these words can appear in values)
    sql = "SELECT * FROM t WHERE note = 'distkey  sortkey  encode'"
    assert rewrite_sql(sql) == sql
    sql2 = "SELECT encode(data, 'base64') FROM t"
    assert rewrite_sql(sql2) == sql2


def test_varchar_max_is_varchar_65535():
    # Redshift's VARCHAR(MAX) is VARCHAR(65535); PostgreSQL has no (max)
    out = rewrite_sql("CREATE TABLE t (id int, bio varchar(max))").lower()
    assert "varchar(max)" not in out and "bio varchar(65535)" in out
    # also outside CREATE TABLE (e.g. ALTER TABLE) and the CHARACTER VARYING form
    alt = rewrite_sql("ALTER TABLE t ADD COLUMN note CHARACTER VARYING(MAX)").lower()
    assert "max" not in alt and "varchar(65535)" in alt


def test_redshift_catalog_columns_rewritten():
    # sqlalchemy-redshift reflection reads Redshift-only pg_catalog columns that
    # PostgreSQL lacks; the proxy answers them with neutral literals.
    sql = (
        "SELECT format_encoding(att.attencodingtype::integer), att.attisdistkey, "
        "att.attsortkeyord, adsrc FROM pg_catalog.pg_attribute att"
    )
    out = rewrite_sql(sql)
    assert "attencodingtype" not in out
    assert "attisdistkey" not in out
    assert "attsortkeyord" not in out
    # attencodingtype -> 0 keeps the ::integer cast; distkey -> false; sortkey -> 0
    assert "format_encoding(0::integer)" in out
    assert "false" in out
    # bare adsrc becomes a named NULL, not left dangling
    assert "NULL::text AS adsrc" in out


def test_reldiststyle_case_rewritten():
    # the relations query wraps c.reldiststyle in a CASE; 0 keeps it valid (EVEN)
    sql = "SELECT CASE c.reldiststyle WHEN 0 THEN 'EVEN' END FROM pg_catalog.pg_class c"
    out = rewrite_sql(sql)
    assert "reldiststyle" not in out
    assert "CASE 0 WHEN 0 THEN 'EVEN' END" in out


def test_catalog_rewrite_is_gated():
    # no Redshift-only marker -> ordinary catalog queries pass through untouched,
    # including the quoted "adsrc" output alias in a Spectrum UNION branch
    sql = 'SELECT n.nspname, null as "adsrc" FROM pg_catalog.pg_namespace n'
    assert rewrite_sql(sql) == sql


def test_where_alias_predicates_translated():
    # output-column aliases used in WHERE become the real columns they alias, so
    # has_table's existence filter keeps working (not just dropped)
    sql = (
        'SELECT n.nspname as "schema", c.relname as "table_name" '
        "FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.reldiststyle = 0 AND n.nspname !~ '^pg_' "
        "AND schema = 'public' AND table_name = 'orders'"
    )
    out = rewrite_sql(sql)
    assert "AND n.nspname = 'public'" in out
    assert "AND c.relname = 'orders'" in out
    assert "!~ '^pg_'" in out  # the real predicate survives


def test_external_union_branches_dropped():
    # oblako has no Spectrum/late-binding catalog, so those UNION branches (which
    # carry the Redshift-only WHERE 1 / svv_* SQL) are dropped, keeping branch 1
    sql = (
        "SELECT c.reldiststyle, n.nspname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname !~ '^pg_' "
        "UNION "
        "SELECT null, s.schemaname FROM svv_external_tables t "
        "JOIN svv_external_schemas s ON s.schemaname = t.schemaname WHERE 1 "
        "ORDER BY 1"
    )
    out = rewrite_sql(sql)
    assert "svv_external" not in out
    assert "WHERE 1" not in out
    assert "UNION" not in out
    assert "n.nspname !~ '^pg_'" in out  # the local-relations branch survives


def test_direct_svv_query_not_emptied():
    # a standalone svv_external_* query (single branch) must survive intact, not
    # be dropped to nothing
    sql = "SELECT count(*) FROM svv_external_tables"
    assert rewrite_sql(sql) == sql


def test_extract_distribution_distkey_and_sortkey():
    # DISTKEY -> create_distributed_table; SORTKEY -> a btree index, in order.
    assert extract_distribution(
        "CREATE TABLE events (id int, user_id int) DISTSTYLE KEY DISTKEY(user_id) SORTKEY(id)"
    ) == [
        "SELECT create_distributed_table('events', 'user_id')",
        "CREATE INDEX ON events (id)",
    ]


def test_extract_distribution_compound_sortkey_multicol():
    cmds = extract_distribution(
        "CREATE TABLE t (id int, k int) DISTKEY(k) COMPOUND SORTKEY (k, id)"
    )
    assert cmds[0] == "SELECT create_distributed_table('t', 'k')"
    assert cmds[1] == "CREATE INDEX ON t (k, id)"


def test_extract_distribution_all_is_reference():
    assert extract_distribution(
        "CREATE TABLE dim (id int, name text) DISTSTYLE ALL"
    ) == ["SELECT create_reference_table('dim')"]


def test_extract_distribution_none_stays_local():
    # EVEN / AUTO / no distkey -> not auto-distributed (stays local on coordinator)
    assert extract_distribution("CREATE TABLE t (a int) DISTSTYLE EVEN") == []
    assert extract_distribution("CREATE TABLE t (a int)") == []
    assert extract_distribution("SELECT * FROM t") == []
    assert extract_distribution("INSERT INTO t VALUES (1)") == []
    # SORTKEY alone (no distribution) leaves the local table untouched
    assert extract_distribution("CREATE TABLE t (a int) SORTKEY(a)") == []


def test_extract_distribution_qualified_and_temp():
    assert extract_distribution(
        "CREATE TABLE IF NOT EXISTS analytics.events (id int, k int) DISTKEY(k)"
    ) == ["SELECT create_distributed_table('analytics.events', 'k')"]
    assert extract_distribution(
        "CREATE TEMP TABLE stg (id int, k int) DISTKEY (k)"
    ) == ["SELECT create_distributed_table('stg', 'k')"]


def test_extract_distribution_is_injection_safe():
    # names are matched as identifier chars only, so no quote/semicolon can escape
    out = extract_distribution("CREATE TABLE t (k int) DISTKEY(k)")[0]
    assert "';" not in out and out.count("'") == 4  # exactly the two quoted args


def _param_status(key: bytes, value: bytes) -> bytes:
    body = key + b"\x00" + value + b"\x00"
    return b"S" + struct.pack("!I", len(body) + 4) + body


def test_server_version_parameter_status_rewritten(monkeypatch):
    # On the Citus variant the proxy presents Redshift's version to the client on
    # the wire (the engine keeps its real version so Citus works).
    monkeypatch.setattr(_mod, "PROXY_SERVER_VERSION", "8.0.2")
    real = b"16.15 (Debian 16.15-1.pgdg12+2)"
    out = _mod._rewrite_parameter_status(b"server_version\x00" + real + b"\x00")
    # message frames correctly and carries the spoofed value
    assert out[:1] == b"S"
    assert struct.unpack("!I", out[1:5])[0] == len(out) - 1
    assert out[5:] == b"server_version\x008.0.2\x00"
    # a different ParameterStatus is passed through untouched
    enc = b"server_encoding\x00UTF8\x00"
    assert (
        _mod._rewrite_parameter_status(enc)
        == b"S" + struct.pack("!I", len(enc) + 4) + enc
    )


# --- redtape / access-management compat (see tests/redshift/test_redshift_redtape.py) ---


def test_usecatupd_neutralized():
    """Bare pg_user.usecatupd (dropped from PG >= 9.5) is answered as a literal.

    redtape's user introspection selects it; the column name is preserved.
    """
    q = (
        "SELECT usename, usesysid, usecreatedb, usesuper, usecatupd, valuntil, "
        "useconfig FROM pg_catalog.pg_user"
    )
    out = rewrite_sql(q)
    assert "false AS usecatupd" in out
    assert "pg_catalog.pg_user" in out  # the table reference is preserved


def test_usecatupd_qualified_is_untouched():
    """A qualified u.usecatupd is a real join column, not the bare select item."""
    out = rewrite_sql("SELECT u.usecatupd FROM pg_catalog.pg_user u")
    assert "false AS usecatupd" not in out


def test_createuser_becomes_superuser():
    """Redshift's one-word CREATEUSER privilege maps to PostgreSQL SUPERUSER."""
    out = rewrite_sql("CREATE USER admin_u PASSWORD 'x' CREATEUSER;")
    assert "SUPERUSER" in out.upper()
    assert "CREATEUSER" not in out.upper()


def test_create_user_two_words_survives():
    """Only the one-word CREATEUSER keyword is rewritten, never "CREATE USER"."""
    out = rewrite_sql("CREATE USER analytics_ro PASSWORD 'x';")
    assert "CREATE USER analytics_ro" in out


def test_pg_group_filters_predefined_roles():
    """pg_catalog.pg_group is wrapped in a subquery that excludes PG pg_* roles."""
    out = rewrite_sql("SELECT groname, grosysid, grolist FROM pg_catalog.pg_group")
    assert "!~ '^pg_'" in out
    assert "pg_catalog.pg_group" in out  # the real table is still the source
    assert out.count("pg_catalog.pg_group") == 1  # replacement is not re-scanned


def test_acl_array_to_string_becomes_redshift_acl():
    """redtape's three ACL reads are pointed at redshift_acl (adds "group ")."""
    for expr in (
        "SELECT array_to_string(pgc.relacl, ','::text)::TEXT AS table_acl",
        "SELECT array_to_string(pgn.nspacl, (',')::text)::TEXT AS schema_acl",
        "SELECT array_to_string(pgd.datacl, (',')::text)::TEXT AS database_acl",
    ):
        out = rewrite_sql(expr)
        assert "redshift_acl(" in out
        assert "array_to_string" not in out
        assert out.endswith(expr[expr.index("acl,") + 3 :])  # arguments untouched


def test_acl_rewrite_consumes_the_pg_catalog_qualifier():
    """sqlalchemy-redshift qualifies the call; redshift_acl lives in public, not there."""
    out = rewrite_sql(
        "SELECT pg_catalog.array_to_string(c.relacl, '\n') AS \"privileges\" "
        "FROM pg_catalog.pg_class c"
    )
    assert "redshift_acl(c.relacl" in out
    assert "pg_catalog.redshift_acl" not in out


def test_acl_rewrite_leaves_other_array_to_string_alone():
    """array_to_string over a non-ACL array is an ordinary call, not rewritten."""
    for expr in (
        "SELECT array_to_string(ARRAY['a','b'], ',')",
        "SELECT array_to_string(t.tags, ',') FROM t",
    ):
        assert rewrite_sql(expr) == expr


def test_password_disable_becomes_password_null():
    """Redshift's PASSWORD DISABLE (IAM-only account) maps to PostgreSQL's NULL."""
    for stmt in (
        "CREATE USER bi_analyst PASSWORD DISABLE;",
        "ALTER USER bi_analyst PASSWORD DISABLE;",
        "create user x password   disable ;",
    ):
        out = rewrite_sql(stmt)
        assert "PASSWORD NULL" in out
        assert "DISABLE" not in out.upper()


def test_password_literal_survives():
    """A real password is untouched, including one that merely contains 'disable'."""
    stmt = "CREATE USER bi_analyst PASSWORD 'disable_me_1';"
    assert rewrite_sql(stmt) == stmt


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("GRANT ROLE analyst TO alice", "GRANT analyst TO alice"),
        ("GRANT ROLE a, ROLE b TO ROLE c", "GRANT a, b TO c"),
        (
            "REVOKE ADMIN OPTION FOR ROLE a FROM alice",
            "REVOKE ADMIN OPTION FOR a FROM alice",
        ),
        ("REVOKE ROLE a FROM ROLE b", "REVOKE a FROM b"),
        (
            "GRANT SELECT ON t TO ROLE analyst, GROUP g",
            "GRANT SELECT ON t TO analyst, GROUP g",
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR USER etl IN SCHEMA s "
            "GRANT SELECT ON TABLES TO ROLE analyst",
            "ALTER DEFAULT PRIVILEGES FOR USER etl IN SCHEMA s "
            "GRANT SELECT ON TABLES TO analyst",
        ),
        # left alone: a system permission, a schema named role, PostgreSQL's form
        ("GRANT CREATE ROLE TO alice", "GRANT CREATE ROLE TO alice"),
        ("GRANT USAGE ON SCHEMA role TO bob", "GRANT USAGE ON SCHEMA role TO bob"),
        ("CREATE ROLE x LOGIN PASSWORD 'p'", "CREATE ROLE x LOGIN PASSWORD 'p'"),
    ],
)
def test_redshift_role_grants_lose_the_role_keyword(sql, expected):
    assert rewrite_sql(sql) == expected


def test_create_role_creates_and_marks_a_redshift_role():
    out = rewrite_sql("CREATE ROLE \"IAM:Ops\" EXTERNALID 'abc';")
    assert out.startswith('DO $oblako_role$ BEGIN CREATE ROLE "IAM:Ops" NOLOGIN; ')
    assert "'IAM:Ops'" in out and "'oblako:redshift-role owner='" in out
    assert out.endswith("END $oblako_role$;")
    # one statement, so it also runs on the extended protocol
    assert "SELECT 1; DO $oblako_role$" in rewrite_sql("SELECT 1; CREATE ROLE r2;")


def test_pg_group_leaves_out_redshift_roles():
    out = rewrite_sql("SELECT groname FROM pg_catalog.pg_group")
    assert "NOT LIKE 'oblako:redshift-role%'" in out
