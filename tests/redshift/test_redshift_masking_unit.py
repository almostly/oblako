"""Unit tests for the masking policy statement rewrite (no services)."""

import importlib.util
import pathlib

_PATH = pathlib.Path(__file__).parents[2] / "oblako/images/redshift/proxy/masking.py"
_spec = importlib.util.spec_from_file_location("_masking", _PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
r = _mod.rewrite_masking


def _call(function: str, args: str, tail: str = ";") -> str:
    return (
        f"DO $oblako_ddm$ BEGIN PERFORM pg_oblako.{function}({args}); "
        f"END $oblako_ddm${tail}"
    )


def test_create_keeps_the_expression_as_one_literal():
    assert r(
        "CREATE MASKING POLICY mask_email WITH (email varchar(256)) "
        "USING (CASE WHEN email LIKE '%@%' THEN '***' ELSE email END);"
    ) == _call(
        "ddm_create",
        "'mask_email', ARRAY['email']::text[], ARRAY['varchar(256)']::text[], "
        "'CASE WHEN email LIKE ''%@%'' THEN ''***'' ELSE email END', false",
    )


def test_create_if_not_exists_and_several_inputs():
    assert r(
        'create masking policy if not exists "Mask" '
        "with (a int, B varchar(10)) using (a + length(b))"
    ) == _call(
        "ddm_create",
        "'Mask', ARRAY['a', 'b']::text[], ARRAY['int', 'varchar(10)']::text[], "
        "'a + length(b)', true",
        "",
    )


def test_alter_and_drop():
    assert r("ALTER MASKING POLICY p USING ('x');") == _call(
        "ddm_alter", "'p', '''x'''"
    )
    assert r("DROP MASKING POLICY dev.p") == _call("ddm_drop", "'p'", "")


def test_attach_to_a_role_with_inputs_and_priority():
    assert r(
        "ATTACH MASKING POLICY p ON public.users(email) USING (email, id) "
        "TO ROLE analyst PRIORITY 10;"
    ) == _call(
        "ddm_attach",
        "'p', 'public.users', ARRAY['email']::text[], ARRAY['email', 'id']::text[], "
        "'analyst', 'role', 10",
    )


def test_attach_to_a_user_and_to_public():
    assert r("ATTACH MASKING POLICY p ON t(c) TO alice") == _call(
        "ddm_attach",
        "'p', 't', ARRAY['c']::text[], ARRAY['c']::text[], 'alice', 'user', 0",
        "",
    )
    assert "'public', 'public', 0" in r("ATTACH MASKING POLICY p ON t(c) TO PUBLIC")


def test_detach():
    assert r("DETACH MASKING POLICY p ON s.t(c) FROM ROLE analyst;") == _call(
        "ddm_detach", "'p', 's.t', ARRAY['c']::text[], 'analyst', 'role'"
    )


def test_forms_redshift_refuses_are_left_as_written():
    for stmt in (
        "ATTACH MASKING POLICY p ON t(c) TO GROUP g",  # a syntax error on Redshift
        "CREATE MASKING POLICY p USING ('x')",
        "DROP MASKING POLICY p CASCADE",
    ):
        assert r(stmt) == stmt


def test_other_statements_are_untouched():
    assert r("SELECT 'masking policy'") == "SELECT 'masking policy'"
