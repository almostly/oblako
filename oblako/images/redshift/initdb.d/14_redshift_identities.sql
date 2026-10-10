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
-- tools that parse the ACL string then file the group as a user, see the
-- group holding nothing, and re-plan the same GRANTs on every run: the apply never
-- converges. Prefixing group grantees here makes the parse agree with Redshift.
--
-- A grantee is a group when oblako_identity_type says so: a role that cannot log
-- in and is not a Redshift role, minus PostgreSQL's predefined pg_* roles (the
-- set the proxy hides from pg_group too). A grant to a Redshift role is left out
-- altogether, as Redshift's own ACL strings leave out RBAC grants: those show
-- only in the SVV views. The grantee is matched unquoted but emitted verbatim, so
-- a quoted name ("IAM:admin") survives intact, and an empty grantee (PUBLIC)
-- matches nothing.
--
-- Signature mirrors array_to_string(acl, sep), which is what the proxy rewrites.
CREATE OR REPLACE FUNCTION pg_catalog.redshift_acl(acl aclitem[], sep text)
    RETURNS text LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN acl IS NULL THEN NULL ELSE coalesce((
        SELECT string_agg(
                 CASE WHEN e.identity = 'group' THEN 'group ' || e.item
                      ELSE e.item END,
                 sep ORDER BY e.ord)
        FROM (SELECT u.entry::text AS item, u.ord,
                     (SELECT pg_catalog.oblako_identity_type(r.oid)
                      FROM pg_catalog.pg_roles r
                      WHERE r.rolname = btrim(split_part(u.entry::text, '=', 1), '"')
                        AND r.rolname !~ '^pg_') AS identity
              FROM unnest(acl) WITH ORDINALITY AS u(entry, ord)) e
        WHERE e.identity IS DISTINCT FROM 'role'
    ), '') END;
$$;

-- Redshift's system-defined roles, as Redshift roles (marked, can't log in), with
-- no owner. Their members are recognized where redshift-local checks for them:
-- the four that can read system tables see every row of the grant views below;
-- sys:dba and sys:superuser may drop schemas and tables (the oblako_redshift
-- extension enforces it, as for a DROP grant); sys:secadmin manages masking
-- policies (15_masking.sql). What a role can't carry in PostgreSQL, such as
-- creating users, still needs a superuser.
DO $sys_roles$
DECLARE
    name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['sys:monitor', 'sys:operator', 'sys:dba',
                                'sys:superuser', 'sys:secadmin'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = name) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', name);
        END IF;
        EXECUTE format('COMMENT ON ROLE %I IS %L', name, 'oblako:redshift-role');
    END LOOP;
END $sys_roles$;
-- the system roles inherit as on Redshift
GRANT "sys:monitor" TO "sys:operator";
GRANT "sys:operator" TO "sys:dba";

-- whether the current user is a member of the named role, if that role exists
CREATE OR REPLACE FUNCTION pg_catalog.oblako_has_role(role_name text)
    RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
                    WHERE r.rolname = role_name
                      AND pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER'))
$$;

-- whether `member` holds `role_oid`, through grants followed role to role. Not
-- pg_has_role, which counts a superuser a member of every role: on Redshift a
-- superuser has only the roles granted to it.
CREATE OR REPLACE FUNCTION pg_catalog.oblako_is_member(member oid, role_oid oid)
    RETURNS boolean LANGUAGE sql STABLE AS $$
    WITH RECURSIVE held(oid) AS (
        SELECT member
        UNION
        SELECT m.roleid FROM pg_catalog.pg_auth_members m JOIN held ON m.member = held.oid)
    SELECT EXISTS (SELECT 1 FROM held WHERE held.oid = role_oid)
$$;

-- Redshift's system permissions granted to roles (GRANT ACCESS SYSTEM TABLE TO
-- ROLE r, which the proxy routes to pg_oblako.system_privilege), read back from
-- svv_system_privileges below
CREATE SCHEMA IF NOT EXISTS pg_oblako;
GRANT USAGE ON SCHEMA pg_oblako TO PUBLIC;
CREATE TABLE IF NOT EXISTS pg_oblako.system_privileges (
    privilege text NOT NULL,
    grantee   oid NOT NULL,
    PRIMARY KEY (privilege, grantee)
);
GRANT SELECT ON pg_oblako.system_privileges TO PUBLIC;

-- whether the current user holds a system permission, through its roles
CREATE OR REPLACE FUNCTION pg_catalog.oblako_holds_system_privilege(priv text)
    RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM pg_oblako.system_privileges p
         WHERE p.privilege = priv
           AND pg_catalog.oblako_is_member(
                   (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user),
                   p.grantee))
$$;

-- GRANT/REVOKE a system permission TO/FROM ROLE r; a superuser grants them
CREATE OR REPLACE FUNCTION pg_oblako.system_privilege(is_grant boolean, priv text, role_name text)
    RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    role_oid oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = role_name);
BEGIN
    IF NOT (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'permission denied to grant system permission %', priv
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF role_oid IS NULL OR pg_catalog.oblako_identity_type(role_oid) <> 'role' THEN
        RAISE EXCEPTION 'role "%" does not exist', role_name USING ERRCODE = 'undefined_object';
    END IF;
    IF is_grant THEN
        INSERT INTO pg_oblako.system_privileges VALUES (priv, role_oid) ON CONFLICT DO NOTHING;
    ELSE
        DELETE FROM pg_oblako.system_privileges p WHERE p.privilege = priv AND p.grantee = role_oid;
    END IF;
END $$;

-- whether the current user sees every row of the grant views: a superuser does,
-- a holder of ACCESS SYSTEM TABLE does, and so do the system roles that have it
CREATE OR REPLACE FUNCTION pg_catalog.oblako_sees_all()
    RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user)
        OR pg_catalog.oblako_has_role('sys:monitor')
        OR pg_catalog.oblako_has_role('sys:superuser')
        OR pg_catalog.oblako_holds_system_privilege('ACCESS SYSTEM TABLE')
$$;

-- USER_IS_MEMBER_OF(user, role or group) and ROLE_IS_MEMBER_OF(role, role or
-- group): membership through grants followed role to role. As on Redshift, asking
-- about another user (or a role the asker doesn't hold) takes a superuser or
-- ACCESS SYSTEM TABLE.
CREATE OR REPLACE FUNCTION pg_catalog.oblako_role_oid(name text, kind text)
    RETURNS oid LANGUAGE plpgsql STABLE AS $$
DECLARE
    found oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = name);
BEGIN
    IF found IS NULL THEN
        RAISE EXCEPTION '% "%" does not exist', kind, name USING ERRCODE = 'undefined_object';
    END IF;
    RETURN found;
END $$;

CREATE OR REPLACE FUNCTION pg_catalog.user_is_member_of(user_name text, role_name text)
    RETURNS boolean LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF user_name <> current_user AND NOT pg_catalog.oblako_sees_all() THEN
        RAISE EXCEPTION 'must be superuser or have ''ACCESS SYSTEM TABLE'' privilege to check membership for another user'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN pg_catalog.oblako_is_member(pg_catalog.oblako_role_oid(user_name, 'user'),
                                       pg_catalog.oblako_role_oid(role_name, 'role'));
END $$;

CREATE OR REPLACE FUNCTION pg_catalog.role_is_member_of(role_name text, granted_role_name text)
    RETURNS boolean LANGUAGE plpgsql STABLE AS $$
DECLARE
    role_oid oid := pg_catalog.oblako_role_oid(role_name, 'role');
BEGIN
    IF NOT pg_catalog.oblako_sees_all()
       AND NOT pg_catalog.oblako_is_member(
               (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user), role_oid) THEN
        RAISE EXCEPTION 'must be superuser or have ''ACCESS SYSTEM TABLE'' privilege to check membership for another user'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN pg_catalog.oblako_is_member(role_oid,
                                       pg_catalog.oblako_role_oid(granted_role_name, 'role'));
END $$;

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
  AND pg_catalog.oblako_identity_type(g.oid) = 'role'
  -- as on Redshift, a user who isn't a superuser sees only its own roles
  AND (pg_catalog.oblako_sees_all() OR u.rolname = current_user);

CREATE OR REPLACE VIEW pg_catalog.svv_role_grants AS
SELECT r.oid::bigint::integer AS role_id,
       r.rolname::text AS role_name,
       g.oid::bigint::integer AS granted_role_id,
       g.rolname::text AS granted_role_name
FROM pg_catalog.pg_auth_members m
JOIN pg_catalog.pg_roles r ON r.oid = m.member
JOIN pg_catalog.pg_roles g ON g.oid = m.roleid
WHERE pg_catalog.oblako_identity_type(r.oid) = 'role'
  AND pg_catalog.oblako_identity_type(g.oid) = 'role'
  -- as on Redshift, a user who isn't a superuser sees the roles it has or owns
  AND (pg_catalog.oblako_sees_all()
       OR pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER')
       OR pg_catalog.shobj_description(r.oid, 'pg_authid')
          = 'oblako:redshift-role owner=' || current_user);

-- DROP ROLE r [ FORCE | RESTRICT ], which the proxy routes here. RESTRICT, the
-- default, refuses a role still granted to a user or a role, or one that holds
-- another role, with Redshift's errors; FORCE removes those assignments first
-- (PostgreSQL's DROP ROLE does that by itself).
CREATE OR REPLACE FUNCTION pg_oblako.drop_role(name text, if_exists boolean, force boolean)
    RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    role_oid oid := (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname = name);
BEGIN
    IF role_oid IS NULL THEN
        IF if_exists THEN
            RAISE NOTICE 'role "%" does not exist, skipping', name;
            RETURN;
        END IF;
        RAISE EXCEPTION 'role "%" does not exist', name USING ERRCODE = 'undefined_object';
    END IF;
    IF NOT force THEN
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
                    WHERE m.roleid = role_oid
                      AND pg_catalog.oblako_identity_type(m.member) = 'user') THEN
            RAISE EXCEPTION 'cannot drop this role since it has been granted on a user'
                USING ERRCODE = 'dependent_objects_still_exist';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
                    WHERE m.roleid = role_oid OR m.member = role_oid) THEN
            RAISE EXCEPTION 'cannot drop this role since it depends on another role'
                USING ERRCODE = 'dependent_objects_still_exist';
        END IF;
    END IF;
    EXECUTE format('DROP ROLE %I', name);
END $$;

-- ALTER and DROP on tables, views and schemas: Redshift privileges PostgreSQL
-- doesn't have. The proxy sends a GRANT or REVOKE of them to
-- pg_oblako.object_privilege, which keeps them here; the privilege views below
-- report them, and the oblako_redshift extension enforces them, running an ALTER
-- or DROP as the object's owner for a user that holds the privilege. Row-level
-- security lets only the object's owner (or a holder with the grant option)
-- grant or revoke; superusers bypass it.
CREATE SCHEMA IF NOT EXISTS pg_oblako;
GRANT USAGE ON SCHEMA pg_oblako TO PUBLIC;
CREATE TABLE IF NOT EXISTS pg_oblako.object_privileges (
    objkind      text NOT NULL,      -- 'relation' or 'schema'
    objid        oid NOT NULL,
    privilege    text NOT NULL,      -- 'ALTER' or 'DROP'
    grantee      oid NOT NULL,       -- 0 for PUBLIC
    grantor      oid NOT NULL,
    admin_option boolean NOT NULL,
    PRIMARY KEY (objkind, objid, privilege, grantee)
);

-- whether `who` holds the privilege on the object, directly, through a role or
-- group, or through PUBLIC; with `grantable`, only with the grant option
CREATE OR REPLACE FUNCTION pg_oblako.holds_object_privilege(
    kind text, obj oid, priv text, who name, grantable boolean DEFAULT false)
    RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS (
        SELECT 1 FROM pg_oblako.object_privileges p
         WHERE p.objkind = kind AND p.objid = obj AND p.privilege = priv
           AND (p.admin_option OR NOT grantable)
           AND (p.grantee = 0 OR pg_catalog.pg_has_role(who, p.grantee, 'MEMBER')))
        -- sys:dba and sys:superuser may drop schemas and tables
        OR (priv = 'DROP' AND NOT grantable AND EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles r
             WHERE r.rolname IN ('sys:dba', 'sys:superuser')
               AND pg_catalog.pg_has_role(who, r.oid, 'MEMBER')))
$$;

-- whether `who` may grant or revoke the privilege: the owner, or a holder with
-- the grant option
CREATE OR REPLACE FUNCTION pg_oblako.may_grant_object_privilege(
    kind text, obj oid, priv text, who name)
    RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT coalesce(pg_catalog.pg_has_role(who, CASE kind
               WHEN 'relation' THEN (SELECT relowner FROM pg_class WHERE oid = obj)
               ELSE (SELECT nspowner FROM pg_namespace WHERE oid = obj) END, 'USAGE'),
           false)
        OR pg_oblako.holds_object_privilege(kind, obj, priv, who, true)
$$;

ALTER TABLE pg_oblako.object_privileges ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS object_privileges_read ON pg_oblako.object_privileges;
DROP POLICY IF EXISTS object_privileges_grant ON pg_oblako.object_privileges;
CREATE POLICY object_privileges_read ON pg_oblako.object_privileges
    FOR SELECT USING (true);
CREATE POLICY object_privileges_grant ON pg_oblako.object_privileges
    FOR ALL USING (pg_oblako.may_grant_object_privilege(objkind, objid, privilege, current_user))
    WITH CHECK (pg_oblako.may_grant_object_privilege(objkind, objid, privilege, current_user));
GRANT SELECT, INSERT, UPDATE, DELETE ON pg_oblako.object_privileges TO PUBLIC;

-- GRANT/REVOKE privileges ON kind objects TO/FROM grantees, for ALTER and DROP.
-- kind is 'relation', 'schema' or 'schema_tables' (ALL TABLES IN SCHEMA); a NULL
-- grantee is PUBLIC.
CREATE OR REPLACE FUNCTION pg_oblako.object_privilege(
    is_grant boolean, privileges text[], kind text, objects text[],
    grantees text[], grant_option boolean)
    RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    name text;
    who text;
    priv text;
    obj oid;
    objs oid[] := '{}';
    grantee_oid oid;
    grantee_oids oid[] := '{}';
    target text := CASE kind WHEN 'schema' THEN 'schema' ELSE 'relation' END;
BEGIN
    FOREACH name IN ARRAY objects LOOP
        IF kind = 'relation' THEN
            obj := to_regclass(name);
            IF obj IS NULL THEN
                RAISE EXCEPTION 'relation "%" does not exist', name USING ERRCODE = 'undefined_table';
            END IF;
            objs := objs || obj;
        ELSE
            obj := to_regnamespace(name);
            IF obj IS NULL THEN
                RAISE EXCEPTION 'schema "%" does not exist', name USING ERRCODE = 'invalid_schema_name';
            END IF;
            IF kind = 'schema' THEN
                objs := objs || obj;
            ELSE
                objs := objs || ARRAY(SELECT c.oid FROM pg_catalog.pg_class c
                                       WHERE c.relnamespace = obj AND c.relkind IN ('r', 'p', 'v', 'm', 'f'));
            END IF;
        END IF;
    END LOOP;
    FOREACH who IN ARRAY grantees LOOP
        IF who IS NULL THEN
            grantee_oids := grantee_oids || 0::oid;
        ELSE
            SELECT r.oid INTO grantee_oid FROM pg_catalog.pg_roles r WHERE r.rolname = who;
            IF grantee_oid IS NULL THEN
                RAISE EXCEPTION 'user "%" does not exist', who USING ERRCODE = 'undefined_object';
            END IF;
            grantee_oids := grantee_oids || grantee_oid;
        END IF;
    END LOOP;
    FOREACH obj IN ARRAY objs LOOP
        FOREACH priv IN ARRAY privileges LOOP
            IF NOT pg_oblako.may_grant_object_privilege(target, obj, priv, current_user) THEN
                RAISE EXCEPTION 'permission denied for %',
                    CASE target WHEN 'schema' THEN 'schema ' || obj::regnamespace::text
                                ELSE 'relation ' || obj::regclass::text END
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            FOREACH grantee_oid IN ARRAY grantee_oids LOOP
                IF is_grant THEN
                    INSERT INTO pg_oblako.object_privileges AS p
                    VALUES (target, obj, priv, grantee_oid,
                            (SELECT r.oid FROM pg_catalog.pg_roles r WHERE r.rolname = current_user),
                            grant_option)
                    ON CONFLICT (objkind, objid, privilege, grantee)
                    DO UPDATE SET admin_option = p.admin_option OR EXCLUDED.admin_option;
                ELSIF grant_option THEN  -- REVOKE GRANT OPTION FOR
                    UPDATE pg_oblako.object_privileges p SET admin_option = false
                     WHERE p.objkind = target AND p.objid = obj AND p.privilege = priv
                       AND p.grantee = grantee_oid;
                ELSE
                    DELETE FROM pg_oblako.object_privileges p
                     WHERE p.objkind = target AND p.objid = obj AND p.privilege = priv
                       AND p.grantee = grantee_oid;
                END IF;
            END LOOP;
        END LOOP;
    END LOOP;
END $$;

-- a dropped table, view or schema takes its ALTER and DROP grants with it
CREATE OR REPLACE FUNCTION pg_oblako.forget_object_privileges()
    RETURNS event_trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    DELETE FROM pg_oblako.object_privileges p
     WHERE (p.objkind = 'relation' AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = p.objid))
        OR (p.objkind = 'schema' AND NOT EXISTS (SELECT 1 FROM pg_namespace n WHERE n.oid = p.objid));
END $$;

DROP EVENT TRIGGER IF EXISTS oblako_forget_object_privileges;
CREATE EVENT TRIGGER oblako_forget_object_privileges ON sql_drop
    EXECUTE FUNCTION pg_oblako.forget_object_privileges();

-- system permissions: the ones granted, and ACCESS SYSTEM TABLE as the system
-- roles hold it. Others see the rows for themselves and their roles.
CREATE OR REPLACE VIEW pg_catalog.svv_system_privileges AS
SELECT v.system_privilege, v.identity_id, v.identity_name, v.identity_type
FROM (
    SELECT p.privilege AS system_privilege, p.grantee::bigint::integer AS identity_id,
           r.rolname::text AS identity_name,
           pg_catalog.oblako_identity_type(p.grantee) AS identity_type, p.grantee AS oid
    FROM pg_oblako.system_privileges p
    JOIN pg_catalog.pg_roles r ON r.oid = p.grantee
    UNION ALL
    SELECT 'ACCESS SYSTEM TABLE', r.oid::bigint::integer, r.rolname::text, 'role', r.oid
    FROM pg_catalog.pg_roles r
    WHERE r.rolname IN ('sys:monitor', 'sys:operator', 'sys:dba', 'sys:superuser')
) v
WHERE pg_catalog.oblako_sees_all()
   OR pg_catalog.oblako_is_member(
          (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user), v.oid);

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
                           'TRUNCATE')
UNION ALL
SELECT n.nspname::text, c.relname::text, p.privilege, p.grantee::bigint::integer,
       pg_catalog.oblako_identity_name(p.grantee),
       pg_catalog.oblako_identity_type(p.grantee),
       p.admin_option AND pg_catalog.oblako_identity_type(p.grantee) IN ('user', 'public')
FROM pg_oblako.object_privileges p
JOIN pg_catalog.pg_class c ON c.oid = p.objid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE p.objkind = 'relation'
  AND (p.grantee = 0 OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.oid = p.grantee));

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
  AND a.grantee <> n.nspowner
UNION ALL
SELECT n.nspname::text, p.privilege, p.grantee::bigint::integer,
       pg_catalog.oblako_identity_name(p.grantee),
       pg_catalog.oblako_identity_type(p.grantee),
       p.admin_option AND pg_catalog.oblako_identity_type(p.grantee) IN ('user', 'public'),
       'SCHEMA'::text
FROM pg_oblako.object_privileges p
JOIN pg_catalog.pg_namespace n ON n.oid = p.objid
WHERE p.objkind = 'schema'
  AND (p.grantee = 0 OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.oid = p.grantee));

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

GRANT SELECT ON pg_catalog.svv_system_privileges TO PUBLIC;
GRANT SELECT ON pg_catalog.svv_roles, pg_catalog.svv_user_grants,
    pg_catalog.svv_role_grants, pg_catalog.svv_relation_privileges,
    pg_catalog.svv_schema_privileges, pg_catalog.svv_database_privileges,
    pg_catalog.svv_function_privileges, pg_catalog.svv_default_privileges
    TO PUBLIC;
