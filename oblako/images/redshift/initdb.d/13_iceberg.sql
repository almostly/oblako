-- Redshift's Apache Iceberg tables: external schemas over the Data Catalog,
-- CREATE TABLE ... USING ICEBERG, and writes with INSERT, UPDATE and DELETE.
--
-- The wire proxy rewrites the Redshift-only DDL into calls to the functions here;
-- the work happens in the importable `iceberg_tables` module (next to the proxy in
-- /usr/local/bin, on plpython's path). An Iceberg table is a view over
-- pg_oblako.iceberg_scan, with INSTEAD OF triggers that stage writes and a
-- deferred trigger that commits them to Iceberg at COMMIT. See
-- proxy/iceberg_tables.py.
--
-- Idempotent: entrypoint.sh re-applies it to every database on each start.

SET allow_system_table_mods = on;
-- every node creates these itself; Citus must not replay them
SET citus.enable_ddl_propagation = off;

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

CREATE SEQUENCE IF NOT EXISTS pg_oblako.iceberg_stage_seq;

-- The work, in Python (proxy/iceberg_tables.py)
CREATE OR REPLACE FUNCTION pg_oblako.create_external_schema_py(
    schema text, database text, create_db boolean, if_not_exists boolean
) RETURNS text LANGUAGE plpython3u AS $$
import iceberg_tables
return iceberg_tables.create_external_schema(plpy, schema, database, create_db, if_not_exists)
$$;

CREATE OR REPLACE FUNCTION pg_oblako.iceberg_create_table_py(
    name text, columns text, location text, partitioned text, properties text,
    if_not_exists boolean, query text
) RETURNS text LANGUAGE plpython3u AS $$
import iceberg_tables
return iceberg_tables.create_table(
    plpy, name, columns, location, partitioned, properties, if_not_exists, query
)
$$;

CREATE OR REPLACE FUNCTION pg_oblako.iceberg_scan(database text, name text)
RETURNS SETOF record LANGUAGE plpython3u AS $$
import iceberg_tables
return iceberg_tables.scan(database, name)
$$;

CREATE OR REPLACE FUNCTION pg_oblako.iceberg_flush_py(stage text)
RETURNS void LANGUAGE plpython3u AS $$
import iceberg_tables
iceberg_tables.commit(plpy, stage)
$$;

CREATE OR REPLACE FUNCTION pg_oblako.show_table_py(name text)
RETURNS text LANGUAGE plpython3u AS $$
import iceberg_tables
return iceberg_tables.show_table(plpy, name)
$$;

CREATE OR REPLACE FUNCTION pg_oblako.iceberg_alter_table_py(name text, action text)
RETURNS text LANGUAGE plpython3u AS $$
import iceberg_tables
return iceberg_tables.alter_table(plpy, name, action)
$$;

CREATE OR REPLACE FUNCTION pg_oblako.iceberg_merge_py(stmt text)
RETURNS text LANGUAGE plpython3u AS $$
import iceberg_tables
return iceberg_tables.merge(plpy, stmt)
$$;

-- Re-raise an error from the Python side as Redshift reports it: the message alone
-- (PL/Python prefixes the exception class, "plpy.Error: "), with its hint and code.
CREATE OR REPLACE FUNCTION pg_oblako.raise_clean(msg text, hint text, state text)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    clean text := regexp_replace(msg, '^(plpy|spiexceptions)\.\w+: ', '');
BEGIN
    IF coalesce(hint, '') = '' THEN
        RAISE EXCEPTION USING MESSAGE = clean, ERRCODE = state;
    END IF;
    RAISE EXCEPTION USING MESSAGE = clean, HINT = hint, ERRCODE = state;
END $$;

-- The entry points the proxy's rewritten SQL calls
CREATE OR REPLACE FUNCTION pg_oblako.create_external_schema(
    schema text, database text, create_db boolean, if_not_exists boolean
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE m text; h text; c text;
BEGIN
    RETURN pg_oblako.create_external_schema_py(schema, database, create_db, if_not_exists);
EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS m = MESSAGE_TEXT, h = PG_EXCEPTION_HINT, c = RETURNED_SQLSTATE;
    PERFORM pg_oblako.raise_clean(m, h, c);
END $$;

CREATE OR REPLACE FUNCTION pg_oblako.iceberg_create_table(
    name text, columns text, location text, partitioned text, properties text,
    if_not_exists boolean, query text
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE m text; h text; c text;
BEGIN
    RETURN pg_oblako.iceberg_create_table_py(
        name, columns, location, partitioned, properties, if_not_exists, query);
EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS m = MESSAGE_TEXT, h = PG_EXCEPTION_HINT, c = RETURNED_SQLSTATE;
    PERFORM pg_oblako.raise_clean(m, h, c);
END $$;

CREATE OR REPLACE FUNCTION pg_oblako.iceberg_alter_table(name text, action text)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE m text; h text; c text;
BEGIN
    RETURN pg_oblako.iceberg_alter_table_py(name, action);
EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS m = MESSAGE_TEXT, h = PG_EXCEPTION_HINT, c = RETURNED_SQLSTATE;
    PERFORM pg_oblako.raise_clean(m, h, c);
END $$;

CREATE OR REPLACE FUNCTION pg_oblako.iceberg_merge(stmt text)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE m text; h text; c text;
BEGIN
    RETURN pg_oblako.iceberg_merge_py(stmt);
EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS m = MESSAGE_TEXT, h = PG_EXCEPTION_HINT, c = RETURNED_SQLSTATE;
    PERFORM pg_oblako.raise_clean(m, h, c);
END $$;

CREATE OR REPLACE FUNCTION pg_oblako.iceberg_flush(stage text)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE m text; h text; c text;
BEGIN
    PERFORM pg_oblako.iceberg_flush_py(stage);
EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS m = MESSAGE_TEXT, h = PG_EXCEPTION_HINT, c = RETURNED_SQLSTATE;
    PERFORM pg_oblako.raise_clean(m, h, c);
END $$;

CREATE OR REPLACE FUNCTION pg_oblako.show_table(name text)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE m text; h text; c text;
BEGIN
    RETURN pg_oblako.show_table_py(name);
EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS m = MESSAGE_TEXT, h = PG_EXCEPTION_HINT, c = RETURNED_SQLSTATE;
    PERFORM pg_oblako.raise_clean(m, h, c);
END $$;

-- Deferred, on each staging table: the first firing at COMMIT writes everything
-- staged and empties the table, so later firings find nothing to do.
CREATE OR REPLACE FUNCTION pg_oblako.iceberg_commit() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    pending boolean;
BEGIN
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %s)', TG_RELID::regclass) INTO pending;
    IF pending THEN
        PERFORM pg_oblako.iceberg_flush(TG_RELID::regclass::text);
    END IF;
    RETURN NULL;
END $$;

-- DROP TABLE on an Iceberg table removes it from the catalog (its files stay).
-- PL/Python has no event triggers, so plpgsql hands the statement over.
CREATE OR REPLACE FUNCTION pg_oblako.iceberg_drop_tables(query text)
RETURNS void LANGUAGE plpython3u AS $$
import iceberg_tables
iceberg_tables.on_drop_table(plpy, query)
$$;

CREATE OR REPLACE FUNCTION pg_oblako.iceberg_on_drop_table() RETURNS event_trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_oblako.iceberg_tables) THEN
        PERFORM pg_oblako.iceberg_drop_tables(current_query());
    END IF;
END $$;

-- A dropped view (DROP TABLE above, DROP VIEW, DROP SCHEMA ... CASCADE) takes its
-- staging table and registry row with it; a dropped schema its registry row.
CREATE OR REPLACE FUNCTION pg_oblako.iceberg_on_sql_drop() RETURNS event_trigger
LANGUAGE plpgsql AS $$
DECLARE
    gone record;
BEGIN
    FOR gone IN
        DELETE FROM pg_oblako.iceberg_tables t
        USING pg_event_trigger_dropped_objects() d
        WHERE d.object_type = 'view' AND d.schema_name = t.schemaname
          AND d.object_name = t.tablename
        RETURNING t.stage
    LOOP
        EXECUTE format('DROP TABLE IF EXISTS %s', gone.stage);
        EXECUTE format('DROP FUNCTION IF EXISTS %s_dml()', gone.stage);
    END LOOP;
    DELETE FROM pg_oblako.external_schemas e
    USING pg_event_trigger_dropped_objects() d
    WHERE d.object_type = 'schema' AND d.object_name = e.schemaname;
END $$;

DO $triggers$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'oblako_iceberg_drop_table') THEN
        CREATE EVENT TRIGGER oblako_iceberg_drop_table ON ddl_command_start
            WHEN TAG IN ('DROP TABLE') EXECUTE FUNCTION pg_oblako.iceberg_on_drop_table();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'oblako_iceberg_sql_drop') THEN
        CREATE EVENT TRIGGER oblako_iceberg_sql_drop ON sql_drop
            WHEN TAG IN ('DROP TABLE', 'DROP VIEW', 'DROP SCHEMA')
            EXECUTE FUNCTION pg_oblako.iceberg_on_sql_drop();
    END IF;
END $triggers$;

-- The svv_external_* views over the registry, as in 05_catalog_views.sql, which a
-- volume from before never re-runs. In pg_catalog once 99_system_catalog.sql has
-- moved them; on a fresh volume in public, where 05 made them.
SELECT set_config(
    'search_path',
    CASE WHEN to_regclass('pg_catalog.svv_external_schemas') IS NOT NULL
         THEN 'pg_catalog' ELSE 'public' END,
    false
);

CREATE OR REPLACE VIEW svv_external_schemas AS
SELECT
    n.oid::integer AS esoid,
    e.schemaname,
    e.databasename,
    NULL::text AS esoptions,
    n.nspowner AS esowner,
    -- external-schema kind; NULL => local (redtape reads it)
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

RESET search_path;
RESET citus.enable_ddl_propagation;
RESET allow_system_table_mods;
