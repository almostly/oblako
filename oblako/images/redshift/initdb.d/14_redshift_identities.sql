-- Redshift's identities and its privilege views.
--
-- Redshift keeps three kinds of identity apart: users, groups and (RBAC) roles.
-- PostgreSQL has one, the role. Here a user is a role that can log in, a group is
-- one that cannot, and a Redshift role is one that cannot log in and carries the
-- shared comment `oblako:redshift-role owner=<creator>`: the proxy turns Redshift's
-- CREATE ROLE into CREATE ROLE ... NOLOGIN plus that comment (PostgreSQL has no
-- event trigger for role DDL, so the marker can't be set from here), and DROP ROLE
-- removes the comment with the role.
--
-- On that model this file defines the SVV views access tools read instead of
-- parsing ACL strings, with Redshift's columns and values:
--   svv_roles, svv_user_grants, svv_role_grants,
--   svv_relation_privileges, svv_schema_privileges, svv_database_privileges,
--   svv_function_privileges, svv_default_privileges.
-- They report explicit grants, as Redshift's do: what an owner holds on its own
-- object is implied by ownership and left out, and so are PostgreSQL privileges
-- Redshift doesn't have (CONNECT, TRIGGER, MAINTAIN). A grant to PUBLIC is
-- identity_type 'public'. admin_option is always false for roles and groups.
--
-- It also defines redshift_acl, the ACL rendering that prefixes group grantees
-- (`group analysts=r/owner`), now without prefixing Redshift roles.
--
-- Everything is created in pg_catalog, where Redshift keeps its system views. The
-- file is idempotent; entrypoint.sh re-applies it to every database on start, so
-- volumes from before it existed get the views too.

SET allow_system_table_mods = on;
-- every node creates these itself; Citus must not replay them
SET citus.enable_ddl_propagation = off;

-- 'public', 'role', 'user' or 'group', as Redshift's identity_type column says
CREATE OR REPLACE FUNCTION pg_catalog.oblako_identity_type(role_oid oid)
    RETURNS text LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN role_oid = 0 THEN 'public'
        WHEN coalesce(pg_catalog.shobj_description(role_oid, 'pg_authid'), '')
             LIKE 'oblako:redshift-role%' THEN 'role'
        WHEN (SELECT r.rolcanlogin FROM pg_catalog.pg_roles r WHERE r.oid = role_oid)
             THEN 'user'
        ELSE 'group'
    END
$$;

CREATE OR REPLACE FUNCTION pg_catalog.oblako_identity_name(role_oid oid)
    RETURNS text LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN role_oid = 0 THEN 'public'
                ELSE pg_catalog.pg_get_userbyid(role_oid)::text END
$$;

-- REDSHIFT_ACL: render an ACL array the way Redshift does, not PostgreSQL. Both
-- store the same aclitem, but Redshift keeps users and groups apart and prefixes
-- a group grantee: `group analysts=r/bi_analyst`. PostgreSQL unified roles and
-- groups in 8.1, so the identical grant reads back `analysts=r/bi_analyst`. Access
-- tools that parse the ACL string (redtape) then file the group as a user, see the
-- group holding nothing, and re-plan the same GRANTs on every run: the apply never
-- converges. Prefixing group grantees here makes the parse agree with Redshift.
--
-- A grantee is a group when oblako_identity_type says so: a role that cannot log
-- in and is not a Redshift role, minus PostgreSQL's predefined pg_* roles (the
-- set the proxy hides from pg_group too). The grantee is matched unquoted but
-- emitted verbatim, so a quoted name ("IAM:admin") survives intact, and an empty
-- grantee (PUBLIC) matches nothing.
--
-- Signature mirrors array_to_string(acl, sep), which is what the proxy rewrites.
CREATE OR REPLACE FUNCTION pg_catalog.redshift_acl(acl aclitem[], sep text)
    RETURNS text LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN acl IS NULL THEN NULL ELSE coalesce((
        SELECT string_agg(
                 CASE WHEN r.rolname IS NULL THEN e.item
                      ELSE 'group ' || e.item END,
                 sep ORDER BY e.ord)
        FROM (SELECT u.entry::text AS item, u.ord
              FROM unnest(acl) WITH ORDINALITY AS u(entry, ord)) e
        LEFT JOIN pg_catalog.pg_roles r
               ON r.rolname = btrim(split_part(e.item, '=', 1), '"')
              AND pg_catalog.oblako_identity_type(r.oid) = 'group'
              AND r.rolname !~ '^pg_'
    ), '') END;
$$;

CREATE OR REPLACE VIEW pg_catalog.svv_roles AS
SELECT r.oid::bigint::integer AS role_id,
       r.rolname::text AS role_name,
       substring(pg_catalog.shobj_description(r.oid, 'pg_authid')
                 FROM 'owner=(.*)$') AS role_owner,
       NULL::text AS external_id
FROM pg_catalog.pg_roles r
WHERE pg_catalog.oblako_identity_type(r.oid) = 'role';

CREATE OR REPLACE VIEW pg_catalog.svv_user_grants AS
SELECT u.oid::bigint::integer AS user_id,
       u.rolname::text AS user_name,
       g.oid::bigint::integer AS role_id,
       g.rolname::text AS role_name,
       m.admin_option AS admin_option
FROM pg_catalog.pg_auth_members m
JOIN pg_catalog.pg_roles u ON u.oid = m.member
JOIN pg_catalog.pg_roles g ON g.oid = m.roleid
WHERE pg_catalog.oblako_identity_type(u.oid) = 'user'
  AND pg_catalog.oblako_identity_type(g.oid) = 'role';

CREATE OR REPLACE VIEW pg_catalog.svv_role_grants AS
SELECT r.oid::bigint::integer AS role_id,
       r.rolname::text AS role_name,
       g.oid::bigint::integer AS granted_role_id,
       g.rolname::text AS granted_role_name
FROM pg_catalog.pg_auth_members m
JOIN pg_catalog.pg_roles r ON r.oid = m.member
JOIN pg_catalog.pg_roles g ON g.oid = m.roleid
WHERE pg_catalog.oblako_identity_type(r.oid) = 'role'
  AND pg_catalog.oblako_identity_type(g.oid) = 'role';

CREATE OR REPLACE VIEW pg_catalog.svv_relation_privileges AS
SELECT n.nspname::text AS namespace_name,
       c.relname::text AS relation_name,
       a.privilege_type::text AS privilege_type,
       a.grantee::bigint::integer AS identity_id,
       pg_catalog.oblako_identity_name(a.grantee) AS identity_name,
       pg_catalog.oblako_identity_type(a.grantee) AS identity_type,
       a.is_grantable
           AND pg_catalog.oblako_identity_type(a.grantee) IN ('user', 'public')
           AS admin_option
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
  AND a.grantee <> c.relowner
  AND a.privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'REFERENCES',
                           'TRUNCATE');

CREATE OR REPLACE VIEW pg_catalog.svv_schema_privileges AS
SELECT n.nspname::text AS namespace_name,
       a.privilege_type::text AS privilege_type,
       a.grantee::bigint::integer AS identity_id,
       pg_catalog.oblako_identity_name(a.grantee) AS identity_name,
       pg_catalog.oblako_identity_type(a.grantee) AS identity_type,
       a.is_grantable
           AND pg_catalog.oblako_identity_type(a.grantee) IN ('user', 'public')
           AS admin_option,
       'SCHEMA'::text AS privilege_scope
FROM pg_catalog.pg_namespace n
CROSS JOIN LATERAL pg_catalog.aclexplode(n.nspacl) a
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
  AND a.grantee <> n.nspowner;

CREATE OR REPLACE VIEW pg_catalog.svv_database_privileges AS
SELECT d.datname::text AS database_name,
       CASE a.privilege_type WHEN 'TEMPORARY' THEN 'TEMP'
                             ELSE a.privilege_type END::text AS privilege_type,
       a.grantee::bigint::integer AS identity_id,
       pg_catalog.oblako_identity_name(a.grantee) AS identity_name,
       pg_catalog.oblako_identity_type(a.grantee) AS identity_type,
       a.is_grantable
           AND pg_catalog.oblako_identity_type(a.grantee) IN ('user', 'public')
           AS admin_option,
       'DATABASE'::text AS privilege_scope
FROM pg_catalog.pg_database d
CROSS JOIN LATERAL pg_catalog.aclexplode(d.datacl) a
WHERE d.datallowconn AND d.datname NOT IN ('template0', 'template1', 'postgres')
  AND a.grantee <> d.datdba
  AND a.privilege_type IN ('CREATE', 'TEMPORARY');

CREATE OR REPLACE VIEW pg_catalog.svv_function_privileges AS
SELECT n.nspname::text AS namespace_name,
       p.proname::text AS function_name,
       pg_catalog.oidvectortypes(p.proargtypes)::text AS argument_types,
       a.privilege_type::text AS privilege_type,
       a.grantee::bigint::integer AS identity_id,
       pg_catalog.oblako_identity_name(a.grantee) AS identity_name,
       pg_catalog.oblako_identity_type(a.grantee) AS identity_type,
       a.is_grantable
           AND pg_catalog.oblako_identity_type(a.grantee) IN ('user', 'public')
           AS admin_option
FROM pg_catalog.pg_proc p
JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) a
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
  AND a.grantee <> p.proowner;

CREATE OR REPLACE VIEW pg_catalog.svv_default_privileges AS
SELECT CASE WHEN d.defaclnamespace = 0 THEN NULL
            ELSE n.nspname::text END AS schema_name,
       CASE d.defaclobjtype WHEN 'r' THEN 'RELATION'
                            ELSE 'FUNCTION' END::text AS object_type,
       d.defaclrole::bigint::integer AS owner_id,
       pg_catalog.pg_get_userbyid(d.defaclrole)::text AS owner_name,
       'user'::text AS owner_type,
       a.privilege_type::text AS privilege_type,
       a.grantee::bigint::integer AS grantee_id,
       pg_catalog.oblako_identity_name(a.grantee) AS grantee_name,
       pg_catalog.oblako_identity_type(a.grantee) AS grantee_type,
       a.is_grantable
           AND pg_catalog.oblako_identity_type(a.grantee) IN ('user', 'public')
           AS admin_option
FROM pg_catalog.pg_default_acl d
LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) a
WHERE d.defaclobjtype IN ('r', 'f')
  AND a.grantee <> d.defaclrole
  AND a.privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'REFERENCES',
                           'TRUNCATE', 'EXECUTE');

GRANT SELECT ON pg_catalog.svv_roles, pg_catalog.svv_user_grants,
    pg_catalog.svv_role_grants, pg_catalog.svv_relation_privileges,
    pg_catalog.svv_schema_privileges, pg_catalog.svv_database_privileges,
    pg_catalog.svv_function_privileges, pg_catalog.svv_default_privileges
    TO PUBLIC;
