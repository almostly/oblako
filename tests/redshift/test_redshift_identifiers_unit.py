"""Unit tests for Redshift's case folding of quoted identifiers (no services)."""

import importlib.util
import pathlib

_PATH = (
    pathlib.Path(__file__).parents[2] / "oblako/images/redshift/proxy/identifiers.py"
)
_spec = importlib.util.spec_from_file_location("_identifiers", _PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def fold(sql: str, sensitive: bool = False) -> str:
    return _mod.fold(sql, sensitive)[0]


def test_quoted_object_names_are_folded():
    assert fold('CREATE TABLE "Sales"."Odd Table" ("Id" int, "x""Y" text)') == (
        'CREATE TABLE "sales"."odd table" ("id" int, "x""y" text)'
    )
    assert fold('SELECT "Id" AS "MyAlias" FROM "S"."T" GROUP BY "C"') == (
        'SELECT "id" AS "myalias" FROM "s"."t" GROUP BY "c"'
    )


def test_strings_dollar_quotes_and_comments_are_left_alone():
    sql = (
        """SELECT '"Keep"', E'it\\'s "Keep"', $f$ "Keep"; $f$ /* "Keep" */ -- "Keep\""""
    )
    assert fold(sql) == sql


def test_user_role_and_group_names_keep_their_case():
    for sql in (
        'CREATE USER "IAM:Alice" PASSWORD DISABLE IN GROUP "Gg"',
        'ALTER GROUP "Gg" ADD USER "Bob", "Carl"',
        'DROP ROLE "Reader"',
        'GRANT ROLE "Reader" TO "IAM:Alice"',
        'SET SESSION AUTHORIZATION "Bob"',
    ):
        assert fold(sql) == sql


def test_grantees_keep_their_case_and_objects_fold():
    assert fold('GRANT USAGE ON SCHEMA "S" TO "Bob", ROLE "Rr", GROUP "Gg"') == (
        'GRANT USAGE ON SCHEMA "s" TO "Bob", ROLE "Rr", GROUP "Gg"'
    )
    assert fold('GRANT SELECT ("Col") ON "T" TO "U"') == (
        'GRANT SELECT ("col") ON "t" TO "U"'
    )
    assert fold('REVOKE SELECT ON "S"."T" FROM "Bob"') == (
        'REVOKE SELECT ON "s"."t" FROM "Bob"'
    )
    assert fold(
        'ALTER DEFAULT PRIVILEGES FOR USER "Owner", "Second" IN SCHEMA "S" '
        'GRANT SELECT ON TABLES TO ROLE "R"'
    ) == (
        'ALTER DEFAULT PRIVILEGES FOR USER "Owner", "Second" IN SCHEMA "s" '
        'GRANT SELECT ON TABLES TO ROLE "R"'
    )
    assert fold('ATTACH MASKING POLICY "P" ON "S"."T"("Email") TO ROLE "R"') == (
        'ATTACH MASKING POLICY "p" ON "s"."t"("email") TO ROLE "R"'
    )


def test_owners_keep_their_case():
    assert fold('ALTER TABLE "S"."T" OWNER TO "IAM:Alice"') == (
        'ALTER TABLE "s"."t" OWNER TO "IAM:Alice"'
    )
    assert fold('CREATE SCHEMA "S" AUTHORIZATION "Bob"') == (
        'CREATE SCHEMA "s" AUTHORIZATION "Bob"'
    )


def test_enable_case_sensitive_identifier_keeps_case_from_the_next_statement():
    sql = 'CREATE TABLE "A" (x int); SET enable_case_sensitive_identifier TO true; '
    sql += 'CREATE TABLE "B" (x int); RESET enable_case_sensitive_identifier; '
    sql += 'CREATE TABLE "C" (x int)'
    out, sensitive = _mod.fold(sql)
    assert '"a"' in out and '"B"' in out and '"c"' in out
    assert sensitive is False
    assert _mod.fold("set enable_case_sensitive_identifier = 'on';")[1] is True
    assert fold('SELECT "Keep"', sensitive=True) == 'SELECT "Keep"'


def test_each_statement_starts_afresh():
    """A grantee list ends with its statement."""
    assert fold('GRANT SELECT ON t TO "Bob"; SELECT "Col" FROM t') == (
        'GRANT SELECT ON t TO "Bob"; SELECT "col" FROM t'
    )
