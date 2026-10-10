"""Unit tests for the GRANT/REVOKE of ALTER and DROP rewrite (no services)."""

import importlib.util
import pathlib

_PATH = (
    pathlib.Path(__file__).parents[2]
    / "oblako/images/redshift/proxy/object_privileges.py"
)
_spec = importlib.util.spec_from_file_location("_object_privileges", _PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
r = _mod.rewrite_object_privileges


def test_the_rest_stays_native_and_alter_drop_go_to_pg_oblako():
    out = r("GRANT USAGE, ALTER ON SCHEMA s TO ROLE r, PUBLIC;")
    assert out == (
        "DO $oblako_priv$ BEGIN GRANT USAGE ON SCHEMA s TO r, PUBLIC; "
        "PERFORM pg_oblako.object_privilege(True, ARRAY['ALTER']::text[], 'schema', "
        "ARRAY['s']::text[], ARRAY['r', NULL]::text[], False); END $oblako_priv$;"
    )


def test_revoke_from_a_quoted_role_on_a_table():
    out = r('REVOKE DROP ON TABLE "s"."t" FROM ROLE "R"')
    assert "BEGIN PERFORM pg_oblako.object_privilege(False, ARRAY['DROP']" in out
    assert "'relation', ARRAY['\"s\".\"t\"']::text[], ARRAY['R']::text[]" in out


def test_all_tables_in_schema_with_grant_option_and_groups():
    out = r("GRANT ALTER ON ALL TABLES IN SCHEMA s TO GROUP g WITH GRANT OPTION")
    assert "'schema_tables', ARRAY['s']::text[], ARRAY['g']::text[], True)" in out


def test_other_statements_are_untouched():
    for stmt in (
        "GRANT SELECT ON t TO r",
        "GRANT DROP USER TO r",
        "GRANT ALTER ON DATABASE d TO r",
        "ALTER TABLE t DROP COLUMN c",
    ):
        assert r(stmt) == stmt
