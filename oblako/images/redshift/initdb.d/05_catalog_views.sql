-- Amazon Redshift catalog views that BI tools / dbt query for metadata
-- (PostgreSQL has no pg_table_def or svv_* views). Mapped onto PostgreSQL's own
-- catalogs / information_schema. They live in pg_catalog, as on Redshift, and
-- resolve unqualified. Pure SQL views, so they also seed template1 for databases
-- created later.

-- Redshift's built-ins live in pg_catalog, so these are created there (see
-- 99_system_catalog.sql); allow_system_table_mods permits it (superuser).
SET allow_system_table_mods = on;
-- every node creates these itself; Citus must not replay them (a worker refuses
-- pg_catalog and pg_ schemas from a replay). A placeholder without Citus.
SET citus.enable_ddl_propagation = off;
SET search_path = pg_catalog, public;

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

-- SVV_EXTERNAL_SCHEMAS / SVV_EXTERNAL_TABLES / SVV_EXTERNAL_COLUMNS: external
-- schemas over the Data Catalog and their Iceberg tables, from oblako's registry
-- (13_iceberg.sql fills it, and defines the same objects on volumes from before).
-- esowner is present so drivers that join it against pg_user (e.g.
-- sqlalchemy-redshift reflection) parse.
CREATE SCHEMA IF NOT EXISTS pg_oblako;

CREATE TABLE IF NOT EXISTS pg_oblako.external_schemas (
    schemaname name PRIMARY KEY,
    databasename name NOT NULL
);

CREATE TABLE IF NOT EXISTS pg_oblako.iceberg_tables (
    schemaname name NOT NULL,
    tablename name NOT NULL,
    databasename name NOT NULL,
    location text,
    stage text NOT NULL UNIQUE,
    PRIMARY KEY (schemaname, tablename)
);

CREATE OR REPLACE VIEW svv_external_schemas AS
SELECT
    n.oid::integer AS esoid,
    e.schemaname,
    e.databasename,
    NULL::text AS esoptions,
    n.nspowner AS esowner,
    -- external-schema kind, as Redshift reports it; NULL => local
    1::smallint AS eskind
FROM pg_oblako.external_schemas e
JOIN pg_namespace n ON n.nspname = e.schemaname;

CREATE OR REPLACE VIEW svv_external_tables AS
SELECT
    t.schemaname,
    t.tablename,
    t.location,
    NULL::text AS input_format,
    NULL::text AS output_format
FROM pg_oblako.iceberg_tables t;

CREATE OR REPLACE VIEW svv_external_columns AS
SELECT
    t.schemaname,
    t.tablename,
    a.attname AS columnname,
    format_type(a.atttypid, a.atttypmod) AS external_type,
    a.attnum::int AS columnnum
FROM pg_oblako.iceberg_tables t
JOIN pg_namespace n ON n.nspname = t.schemaname
JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = t.tablename
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped;

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

-- redshift_acl (Redshift-style ACL strings) is defined in 14_redshift_identities.sql,
-- beside the identity model it depends on.

-- Own the public schema by the admin user. PostgreSQL 15+ owns public by the
-- pg_database_owner predefined role, but Redshift has no such role: schemas are
-- owned by real users, and access tools that map a schema's owner to a user
-- (pgsesame joins owners to pg_user) find nobody for an owner that isn't one.
-- CURRENT_USER is the bootstrap admin.
ALTER SCHEMA public OWNER TO CURRENT_USER;

-- back to the session defaults, for whoever runs this file next in the session
RESET search_path;
RESET allow_system_table_mods;
RESET citus.enable_ddl_propagation;
