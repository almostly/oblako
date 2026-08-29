-- Amazon Redshift catalog views that BI tools / dbt query for metadata
-- (PostgreSQL has no pg_table_def or svv_* views). Mapped onto PostgreSQL's own
-- catalogs / information_schema. They live in the public schema and resolve
-- unqualified, since none of these names exist in pg_catalog. Pure SQL views, so
-- they also seed template1 for databases created later.

-- PG_TABLE_DEF: one row per column of each user table/view.
CREATE OR REPLACE VIEW pg_table_def AS
SELECT
    n.nspname                               AS schemaname,
    c.relname                               AS tablename,
    a.attname                               AS "column",
    format_type(a.atttypid, a.atttypmod)    AS type,
    'none'::text                            AS encoding,
    false                                   AS distkey,
    0::smallint                             AS sortkey,
    a.attnotnull                            AS notnull
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND c.relkind IN ('r', 'v', 'm', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast');

-- SVV_TABLES: the table catalog (local + would-be external).
CREATE OR REPLACE VIEW svv_tables AS
SELECT
    table_catalog,
    table_schema,
    table_name,
    table_type,
    NULL::text AS remarks
FROM information_schema.tables;

-- SVV_COLUMNS: the column catalog.
CREATE OR REPLACE VIEW svv_columns AS
SELECT
    table_catalog,
    table_schema,
    table_name,
    column_name,
    ordinal_position,
    column_default,
    is_nullable,
    data_type,
    character_maximum_length,
    numeric_precision,
    numeric_scale,
    NULL::text AS remarks
FROM information_schema.columns;

-- SVV_TABLE_INFO: per-table stats. Redshift exposes distribution/sort/skew here;
-- on PostgreSQL those don't exist, so report neutral values + the live row count.
CREATE OR REPLACE VIEW svv_table_info AS
SELECT
    current_database()                AS database,
    n.nspname                         AS schema,
    c.oid::int                        AS table_id,
    c.relname                         AS "table",
    'EVEN'::text                      AS diststyle,
    NULL::text                        AS sortkey1,
    0::int                            AS size,
    0::numeric                        AS pct_used,
    0::numeric                        AS unsorted,
    0::numeric                        AS stats_off,
    GREATEST(c.reltuples, 0)::bigint  AS tbl_rows,
    GREATEST(c.reltuples, 0)::bigint  AS estimated_visible_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast');

-- SVV_EXTERNAL_SCHEMAS / SVV_EXTERNAL_TABLES / SVV_EXTERNAL_COLUMNS: Redshift
-- Spectrum. oblako has no external catalog, so these are empty (tools probe
-- them, then find nothing). esowner is present so drivers that join it against
-- pg_user (e.g. sqlalchemy-redshift reflection) parse.
CREATE OR REPLACE VIEW svv_external_schemas AS
SELECT
    NULL::integer AS esoid,
    NULL::name AS schemaname,
    NULL::name AS databasename,
    NULL::text AS esoptions,
    NULL::oid AS esowner,
    -- external-schema kind; NULL => local (redtape reads it)
    NULL::smallint AS eskind
WHERE false;

CREATE OR REPLACE VIEW svv_external_tables AS
SELECT
    NULL::name AS schemaname,
    NULL::name AS tablename,
    NULL::text AS location,
    NULL::text AS input_format,
    NULL::text AS output_format
WHERE false;

CREATE OR REPLACE VIEW svv_external_columns AS
SELECT
    NULL::name AS schemaname,
    NULL::name AS tablename,
    NULL::name AS columnname,
    NULL::text AS external_type,
    NULL::int  AS columnnum
WHERE false;

-- FORMAT_ENCODING: Redshift maps a column's compression-encoding id to its name.
-- oblako's engine has no column encodings, so every column reads back as 'none'.
-- (sqlalchemy-redshift's reflection wraps att.attencodingtype in this.)
CREATE OR REPLACE FUNCTION format_encoding(integer)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT 'none'::text $$;

-- PG_GET_LATE_BINDING_VIEW_COLS: Redshift lists columns of late-binding views.
-- oblako has none, so this is an empty set. It returns SETOF record, so callers
-- supply the column list (as sqlalchemy-redshift's reflection does).
CREATE OR REPLACE FUNCTION pg_get_late_binding_view_cols()
    RETURNS SETOF record LANGUAGE plpgsql AS $$ BEGIN RETURN; END $$;

-- Access-management compat: the below let Redshift access tools (e.g. redtape,
-- which manages users/groups/grants as code) introspect on oblako unchanged.

-- LIKE_ESCAPE: Redshift rewrites a LIKE pattern's custom escape char to the
-- engine's backslash escape, e.g. like_escape('pg!_temp!_%', '!') -> 'pg\_temp\_%'.
-- redtape's table introspection uses it to flag temp schemas.
CREATE OR REPLACE FUNCTION like_escape(pattern text, escape_char text)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT replace(replace(replace(
             pattern, escape_char || '_', E'\\_'),
             escape_char || '%', E'\\%'),
             escape_char || escape_char, escape_char);
$$;

-- PG_GET_SHARED_REDSHIFT_SCHEMAS / PG_GET_ALL_EXTERNAL_SCHEMAS: Redshift data-
-- sharing and external (Spectrum) schema catalogs. oblako has neither, so both
-- are empty. RETURNS SETOF record, so callers supply the column list (redtape's
-- schema introspection UNION-ALLs these in). Mirrors pg_get_late_binding_view_cols.
CREATE OR REPLACE FUNCTION pg_get_shared_redshift_schemas()
    RETURNS SETOF record LANGUAGE plpgsql AS $$ BEGIN RETURN; END $$;

CREATE OR REPLACE FUNCTION pg_get_all_external_schemas()
    RETURNS SETOF record LANGUAGE plpgsql AS $$ BEGIN RETURN; END $$;

-- REDSHIFT_ACL: render an ACL array the way Redshift does, not PostgreSQL. Both
-- store the same aclitem, but Redshift keeps users and groups apart and prefixes
-- a group grantee: `group analysts=r/bi_analyst`. PostgreSQL unified roles and
-- groups in 8.1, so the identical grant reads back `analysts=r/bi_analyst`. Access
-- tools that parse the ACL string (redtape) then file the group as a user, see the
-- group holding nothing, and re-plan the same GRANTs on every run: the apply never
-- converges. Prefixing group grantees here makes the parse agree with Redshift.
--
-- A grantee is a group when it is a role that cannot log in, minus PostgreSQL's
-- predefined pg_* roles: the same predicate pg_group is defined by, and the same
-- set the proxy leaves visible there, so the two answers to "what is a group" stay
-- consistent. Spelt against pg_roles rather than pg_group because the proxy
-- rewrites pg_catalog.pg_group into a subquery, which would corrupt this body if
-- the file were ever re-applied through the proxy (the tests do exactly that).
-- The grantee is matched unquoted but emitted verbatim, so a quoted name
-- ("IAM:admin") survives intact, and an empty grantee (PUBLIC) matches nothing.
--
-- Signature mirrors array_to_string(acl, sep), which is what the proxy rewrites.
CREATE OR REPLACE FUNCTION redshift_acl(acl aclitem[], sep text)
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
              AND NOT r.rolcanlogin
              AND r.rolname !~ '^pg_'
    ), '') END;
$$;

-- Own the public schema by the admin user. PostgreSQL 15+ owns public by the
-- pg_database_owner predefined role, but Redshift has no such role: schemas are
-- owned by real users, and tools that map a schema's owner to a user (redtape)
-- fail on an owner that isn't in pg_user. CURRENT_USER is the bootstrap admin.
ALTER SCHEMA public OWNER TO CURRENT_USER;
