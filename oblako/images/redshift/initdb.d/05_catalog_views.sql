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
    NULL::name    AS schemaname,
    NULL::name    AS databasename,
    NULL::text    AS esoptions,
    NULL::oid     AS esowner
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
