-- Redshift dynamic data masking (DDM), the catalog side.
--
-- The proxy (proxy/masking.py) turns CREATE / ALTER / DROP / ATTACH / DETACH
-- MASKING POLICY into calls to the ddm_* functions below, which keep policies and
-- attachments in pg_oblako tables and enforce what Redshift Serverless enforces
-- (checked 2026-10-07):
--   * a policy name is unique; DROP refuses while the policy is attached;
--   * an expression of ambiguous type (a bare literal, '***') is refused, on
--     CREATE and ALTER: it needs a cast ('***'::varchar(256));
--   * ALTER changes only the expression, and not its output type, compared
--     exactly (varchar(64) and varchar(10) clash, as do varchar and text);
--   * on one column two different policies can't share a priority, one policy
--     can be attached to several grantees at one priority, and to one grantee at
--     several priorities; DETACH removes all of a grantee's attachments of it;
--   * a role attached to a policy at the priority PUBLIC holds it at replaces
--     PUBLIC's attachment (checked 2026-10-10);
--   * input types are not checked against the column on attach;
--   * dropping a table (or its schema) drops its attachments.
-- svv_masking_policy and svv_attached_masking_policy answer with Redshift's
-- columns and its JSON text formats, to superusers only (others see no rows).
-- An expression is stored as written: Redshift keeps its own normalised form,
-- which access tools compare by round trip, not by text.
--
-- It also adds svv_column_privileges (column-level grants). Idempotent; the
-- entrypoint re-applies it to every database on start.

SET allow_system_table_mods = on;
SET citus.enable_ddl_propagation = off;
CREATE SCHEMA IF NOT EXISTS pg_oblako;

CREATE TABLE IF NOT EXISTS pg_oblako.ddm_policies (
    name           text PRIMARY KEY,
    input_names    text[] NOT NULL,
    input_types    text[] NOT NULL,
    expression     text NOT NULL,
    output_type    text NOT NULL,
    modified_by    text NOT NULL,
    modified_time  timestamp NOT NULL
);

CREATE TABLE IF NOT EXISTS pg_oblako.ddm_attachments (
    policy         text NOT NULL REFERENCES pg_oblako.ddm_policies(name),
    relid          oid NOT NULL,
    schema_name    text NOT NULL,
    table_name     text NOT NULL,
    table_type     text NOT NULL,
    grantor        text NOT NULL,
    grantee        text NOT NULL,
    grantee_type   text NOT NULL,
    priority       integer NOT NULL,
    input_columns  text[] NOT NULL,
    output_columns text[] NOT NULL
);

CREATE OR REPLACE FUNCTION pg_oblako.ddm_require_superuser()
    RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF NOT (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'permission denied: only superusers and users with sys:secadmin can manage masking policies';
    END IF;
END $$;

-- the type an expression produces over the policy's inputs, as Redshift names it
-- (character varying(256), text, integer ...): a throwaway view over typed NULLs
CREATE OR REPLACE FUNCTION pg_oblako.ddm_output_type(
    policy text, names text[], types text[], expression text)
    RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    cols text;
    result text;
BEGIN
    SELECT string_agg(format('NULL::%s AS %I', t, n), ', ')
      INTO cols FROM unnest(names, types) AS u(n, t);
    -- a bare literal has no type until something gives it one; Redshift refuses it
    EXECUTE format('SELECT pg_typeof((%s))::text FROM (SELECT %s) AS masked_table',
                   expression, cols) INTO result;
    IF result = 'unknown' THEN
        RAISE EXCEPTION 'CREATE MASKING POLICY "%" with ambiguous type is not supported', policy
            USING ERRCODE = 'feature_not_supported',
                  HINT = 'The masking expression requires type casting';
    END IF;
    EXECUTE format('CREATE TEMP VIEW oblako_ddm_probe AS SELECT (%s) AS out FROM (SELECT %s) AS masked_table',
                   expression, cols);
    SELECT format_type(a.atttypid, a.atttypmod) INTO result
      FROM pg_catalog.pg_attribute a
     WHERE a.attrelid = 'oblako_ddm_probe'::regclass AND a.attname = 'out';
    DROP VIEW oblako_ddm_probe;
    RETURN result;
END $$;

-- Each policy as a SQL function over its inputs, so a query can apply it:
-- pg_oblako."ddm_fn_<policy>"(inputs) RETURNS output type. The expression sees
-- its inputs by name and as masked_table.<name>, as Redshift's does.
CREATE OR REPLACE FUNCTION pg_oblako.ddm_define_function(policy text)
    RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    p pg_oblako.ddm_policies;
    params text;
    cols text;
BEGIN
    SELECT * INTO p FROM pg_oblako.ddm_policies WHERE name = policy;
    SELECT string_agg(format('%I %s', 'oblako_in_' || i, t), ', ' ORDER BY i),
           string_agg(format('%I AS %I', 'oblako_in_' || i, n), ', ' ORDER BY i)
      INTO params, cols
      FROM unnest(p.input_names, p.input_types) WITH ORDINALITY AS u(n, t, i);
    IF to_regproc(format('pg_oblako.%I', 'ddm_fn_' || policy)) IS NOT NULL THEN
        EXECUTE format('DROP FUNCTION pg_oblako.%I', 'ddm_fn_' || policy);
    END IF;
    EXECUTE format(
        'CREATE FUNCTION pg_oblako.%I(%s) RETURNS %s LANGUAGE sql IMMUTABLE AS %L',
        'ddm_fn_' || policy, params, p.output_type,
        format('SELECT (%s) FROM (SELECT %s) AS masked_table', p.expression, cols));
    EXECUTE format('GRANT EXECUTE ON FUNCTION pg_oblako.%I TO PUBLIC', 'ddm_fn_' || policy);
END $$;

CREATE OR REPLACE FUNCTION pg_oblako.ddm_create(
    policy text, names text[], types text[], expression text, if_not_exists boolean)
    RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_oblako.ddm_require_superuser();
    IF EXISTS (SELECT 1 FROM pg_oblako.ddm_policies WHERE name = policy) THEN
        IF if_not_exists THEN RETURN; END IF;
        RAISE EXCEPTION 'masking policy "%" already exists', policy;
    END IF;
    INSERT INTO pg_oblako.ddm_policies VALUES (
        policy, names,
        ARRAY(SELECT format_type(t::regtype, NULL) || coalesce(
                  substring(t FROM '\([0-9, ]+\)$'), '') FROM unnest(types) AS t),
        expression, pg_oblako.ddm_output_type(policy, names, types, expression),
        current_user, now()::timestamp);
    PERFORM pg_oblako.ddm_define_function(policy);
END $$;

CREATE OR REPLACE FUNCTION pg_oblako.ddm_alter(policy text, expression text)
    RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    p pg_oblako.ddm_policies;
    new_type text;
BEGIN
    PERFORM pg_oblako.ddm_require_superuser();
    SELECT * INTO p FROM pg_oblako.ddm_policies WHERE name = policy;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'masking policy "%" does not exist', policy;
    END IF;
    new_type := pg_oblako.ddm_output_type(policy, p.input_names, p.input_types, expression);
    IF new_type <> p.output_type THEN
        RAISE EXCEPTION 'The expressions at position 0 have different types in the masking policy % and the new policy: "%" and "%"',
            policy, p.output_type, new_type;
    END IF;
    UPDATE pg_oblako.ddm_policies
       SET expression = ddm_alter.expression, modified_by = current_user,
           modified_time = now()::timestamp
     WHERE name = policy;
    PERFORM pg_oblako.ddm_define_function(policy);
END $$;

CREATE OR REPLACE FUNCTION pg_oblako.ddm_drop(policy text)
    RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_oblako.ddm_require_superuser();
    IF NOT EXISTS (SELECT 1 FROM pg_oblako.ddm_policies WHERE name = policy) THEN
        RAISE EXCEPTION 'masking policy "%" does not exist', policy;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_oblako.ddm_attachments WHERE ddm_attachments.policy = ddm_drop.policy) THEN
        RAISE EXCEPTION 'cannot drop masking policy % because other objects depend on it', policy;
    END IF;
    DELETE FROM pg_oblako.ddm_policies WHERE name = policy;
    IF to_regproc(format('pg_oblako.%I', 'ddm_fn_' || policy)) IS NOT NULL THEN
        EXECUTE format('DROP FUNCTION pg_oblako.%I', 'ddm_fn_' || policy);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION pg_oblako.ddm_attach(
    policy text, relation text, outputs text[], inputs text[],
    grantee text, grantee_type text, priority integer)
    RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    rel oid;
    clash text;
    col text;
BEGIN
    PERFORM pg_oblako.ddm_require_superuser();
    IF NOT EXISTS (SELECT 1 FROM pg_oblako.ddm_policies WHERE name = policy) THEN
        RAISE EXCEPTION 'masking policy "%" does not exist', policy;
    END IF;
    rel := to_regclass(relation);
    IF rel IS NULL THEN
        RAISE EXCEPTION 'relation "%" does not exist', relation;
    END IF;
    FOREACH col IN ARRAY outputs || inputs LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute
                        WHERE attrelid = rel AND attname = col AND attnum > 0
                          AND NOT attisdropped) THEN
            RAISE EXCEPTION 'column "%" does not exist in relation "%"', col, relation;
        END IF;
    END LOOP;
    IF grantee_type <> 'public' AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = grantee) THEN
        RAISE EXCEPTION '% "%" does not exist', grantee_type, grantee;
    END IF;
    SELECT a.policy INTO clash FROM pg_oblako.ddm_attachments a
     WHERE a.relid = rel AND a.output_columns && outputs
       AND a.priority = ddm_attach.priority AND a.policy <> ddm_attach.policy
     LIMIT 1;
    IF clash IS NOT NULL THEN
        RAISE EXCEPTION 'DDM policy "%" is already attached on relation "%" for given column with same priority',
            clash, (SELECT relname FROM pg_catalog.pg_class WHERE oid = rel);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_oblako.ddm_attachments a
                WHERE a.relid = rel AND a.output_columns = outputs AND a.priority = ddm_attach.priority
                  AND a.policy = ddm_attach.policy AND a.grantee = ddm_attach.grantee
                  AND a.grantee_type = ddm_attach.grantee_type) THEN
        RAISE EXCEPTION 'DDM policy "%" is already attached on relation "%" for given column, grantee and priority',
            policy, (SELECT relname FROM pg_catalog.pg_class WHERE oid = rel);
    END IF;
    -- a role attaching the policy at PUBLIC's priority takes PUBLIC's place
    IF grantee_type = 'role' THEN
        DELETE FROM pg_oblako.ddm_attachments a
         WHERE a.relid = rel AND a.output_columns = outputs AND a.priority = ddm_attach.priority
           AND a.policy = ddm_attach.policy AND a.grantee_type = 'public';
    END IF;
    INSERT INTO pg_oblako.ddm_attachments
    SELECT policy, rel, n.nspname, c.relname,
           CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' ELSE 'table' END,
           current_user, grantee, grantee_type, priority, inputs, outputs
      FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE c.oid = rel;
END $$;

CREATE OR REPLACE FUNCTION pg_oblako.ddm_detach(
    policy text, relation text, outputs text[], grantee text, grantee_type text)
    RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    rel oid;
    removed integer;
BEGIN
    PERFORM pg_oblako.ddm_require_superuser();
    rel := to_regclass(relation);
    IF rel IS NULL THEN
        RAISE EXCEPTION 'relation "%" does not exist', relation;
    END IF;
    DELETE FROM pg_oblako.ddm_attachments a
     WHERE a.policy = ddm_detach.policy AND a.relid = rel AND a.output_columns = outputs
       AND a.grantee = ddm_detach.grantee AND a.grantee_type = ddm_detach.grantee_type;
    GET DIAGNOSTICS removed = ROW_COUNT;
    IF removed = 0 THEN
        RAISE EXCEPTION 'masking policy "%" is not attached to the given column and grantee', policy;
    END IF;
END $$;

-- a dropped table (or schema) takes its attachments with it; as the catalog's
-- owner, since any user may drop its own table
CREATE OR REPLACE FUNCTION pg_oblako.ddm_forget_dropped()
    RETURNS event_trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    DELETE FROM pg_oblako.ddm_attachments a
     WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.oid = a.relid);
END $$;

DROP EVENT TRIGGER IF EXISTS oblako_ddm_forget_dropped;
CREATE EVENT TRIGGER oblako_ddm_forget_dropped ON sql_drop
    EXECUTE FUNCTION pg_oblako.ddm_forget_dropped();

-- Query time. The proxy replaces a masked table read in a query with the SELECT
-- ddm_masked_select returns: each masked column becomes a CASE on the policy that
-- applies to the current user, the highest priority among the attachments to
-- that user, to a role granted to it (directly or through other roles) and to
-- PUBLIC. A superuser is masked like anyone else, as on Redshift.
-- The choice is an uncorrelated subquery, so it is made once per query, not per
-- row, and it follows SET ROLE as Redshift's does.
CREATE OR REPLACE FUNCTION pg_oblako.ddm_policy_for(rel oid, col text, who name)
    RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT a.policy FROM pg_oblako.ddm_attachments a
     WHERE a.relid = rel AND col = ANY (a.output_columns)
       AND (a.grantee_type = 'public'
            OR (a.grantee_type = 'user' AND a.grantee = who)
            OR (a.grantee_type = 'role' AND a.grantee IN (
                -- the roles granted to the user, directly or through other
                -- roles; not pg_has_role, which counts a superuser a member of
                -- every role (on Redshift a superuser has only its grants)
                WITH RECURSIVE held(oid) AS (
                    SELECT m.roleid FROM pg_auth_members m
                      JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = who
                    UNION
                    SELECT m.roleid FROM pg_auth_members m JOIN held ON m.member = held.oid)
                SELECT r.rolname FROM held JOIN pg_roles r ON r.oid = held.oid)))
     ORDER BY a.priority DESC
     LIMIT 1
$$;
GRANT EXECUTE ON FUNCTION pg_oblako.ddm_policy_for(oid, text, name) TO PUBLIC;

-- for one user: the columns it may read (Redshift expands * to those and refuses
-- a named other one), or, if none, the plain table so the read is refused
DROP FUNCTION IF EXISTS pg_oblako.ddm_masked_select(oid);
CREATE OR REPLACE FUNCTION pg_oblako.ddm_masked_select(rel oid, who name)
    RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
    cols text;
BEGIN
    SELECT string_agg(
             CASE WHEN m.attname IS NULL THEN quote_ident(a.attname)
                  ELSE format('CASE (SELECT pg_oblako.ddm_policy_for(%s, %L, current_user)) %s ELSE %I END AS %I',
                              rel, a.attname, m.branches, a.attname, a.attname)
             END, ', ' ORDER BY a.attnum)
      INTO cols
      FROM pg_catalog.pg_attribute a
      LEFT JOIN LATERAL (
          SELECT a.attname, string_agg(
                   format('WHEN %L THEN pg_oblako.%I(%s)::%s', x.policy, 'ddm_fn_' || x.policy,
                          (SELECT string_agg(quote_ident(c), ', ') FROM unnest(x.input_columns) AS c),
                          format_type(a.atttypid, a.atttypmod)),
                   ' ' ORDER BY x.policy) AS branches
            FROM (SELECT DISTINCT ON (d.policy) d.policy, d.input_columns
                    FROM pg_oblako.ddm_attachments d
                   WHERE d.relid = rel AND a.attname = ANY (d.output_columns)
                   ORDER BY d.policy, d.priority DESC) x
          HAVING count(*) > 0
      ) m ON true
     WHERE a.attrelid = rel AND a.attnum > 0 AND NOT a.attisdropped
       AND has_column_privilege(who, rel, a.attnum, 'SELECT');
    IF cols IS NULL THEN
        RETURN format('SELECT * FROM %s', rel::regclass);
    END IF;
    RETURN format('SELECT %s FROM %s', cols, rel::regclass);
END $$;

-- the masked tables of this database, and the SELECT that stands in for each, for
-- one user
DROP FUNCTION IF EXISTS pg_oblako.ddm_masked_tables();
CREATE OR REPLACE FUNCTION pg_oblako.ddm_masked_tables(who name)
    RETURNS TABLE (schema_name text, table_name text, masked_select text)
    LANGUAGE sql STABLE AS $$
    SELECT n.nspname::text, c.relname::text, pg_oblako.ddm_masked_select(c.oid, who)
      FROM (SELECT DISTINCT relid FROM pg_oblako.ddm_attachments) a
      JOIN pg_catalog.pg_class c ON c.oid = a.relid
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
$$;

-- Redshift's JSON text, built by hand to match its spacing and key order
CREATE OR REPLACE VIEW pg_catalog.svv_masking_policy AS
SELECT current_database()::text AS policy_database,
       p.name AS policy_name,
       '[' || (SELECT string_agg(format('{"colname":%s,"type":%s}', to_json(n), to_json(t)), ',')
                 FROM unnest(p.input_names, p.input_types) AS u(n, t)) || ']' AS input_columns,
       format('[{"expr":%s,"type":%s}]', to_json(p.expression), to_json(p.output_type)) AS policy_expression,
       p.modified_by AS policy_modified_by,
       p.modified_time AS policy_modified_time
FROM pg_oblako.ddm_policies p
WHERE (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user);

CREATE OR REPLACE VIEW pg_catalog.svv_attached_masking_policy AS
SELECT a.policy AS policy_name,
       a.schema_name,
       a.table_name,
       a.table_type,
       a.grantor,
       a.grantee,
       a.grantee_type,
       a.priority,
       (SELECT '[' || string_agg(to_json(c)::text, ',') || ']' FROM unnest(a.input_columns) AS c) AS input_columns,
       (SELECT '[' || string_agg(to_json(c)::text, ',') || ']' FROM unnest(a.output_columns) AS c) AS output_columns,
       false AS is_masking_datashare_on
FROM pg_oblako.ddm_attachments a
WHERE (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user);

-- column-level grants, as Redshift lists them
CREATE OR REPLACE VIEW pg_catalog.svv_column_privileges AS
SELECT n.nspname::text AS namespace_name,
       c.relname::text AS relation_name,
       a.attname::text AS column_name,
       x.privilege_type::text AS privilege_type,
       x.grantee::bigint::integer AS identity_id,
       pg_catalog.oblako_identity_name(x.grantee) AS identity_name,
       pg_catalog.oblako_identity_type(x.grantee) AS identity_type
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) x
WHERE a.attnum > 0 AND NOT a.attisdropped
  AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema';

-- policies kept before query masking existed get their functions
SELECT pg_oblako.ddm_define_function(name) FROM pg_oblako.ddm_policies;

GRANT SELECT ON pg_catalog.svv_masking_policy, pg_catalog.svv_attached_masking_policy,
    pg_catalog.svv_column_privileges TO PUBLIC;
GRANT USAGE ON SCHEMA pg_oblako TO PUBLIC;
