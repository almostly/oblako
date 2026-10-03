-- Move oblako's Redshift system objects out of the public schema into pg_catalog,
-- where Redshift keeps its STL/STV tables, SVV views and built-in functions.
--
-- In public they looked like user objects: SQLAlchemy reflection listed stl_scan
-- and friends as tables, so Alembic's autogenerate proposed dropping them, and
-- schema browsers and dbt docs showed them beside the user's own tables. In
-- pg_catalog they resolve exactly as before (it is always searched first) and
-- tools treat them as the system's.
--
-- The earlier scripts create everything in public; this one runs last.
-- It also moves oblako's internal schemas, on volumes from before, to pg_oblako. It is
-- idempotent, and entrypoint.sh re-applies it to every database on each start, so
-- it also migrates volumes created before it existed (and template1, so databases
-- created later inherit the objects in pg_catalog).

SET allow_system_table_mods = on;
-- every node creates these itself; Citus must not replay them (a worker refuses
-- pg_catalog and pg_ schemas from a replay). A placeholder without Citus.
SET citus.enable_ddl_propagation = off;

DO $migrate$
DECLARE
    rel text;
    fn text;
    old text;
    obj record;
BEGIN
    -- oblako's internal objects (the Redshift ML model table, the integer avg
    -- overloads) live in one schema, pg_oblako: the pg_ prefix is the one Redshift
    -- uses for its own (pg_internal), and SQLAlchemy and Alembic skip it. On volumes
    -- from before, each object moves over from oblako_ml and redshift_compat. Moving
    -- keeps what depends on it: user views that the proxy wrote with
    -- redshift_compat.avg follow the aggregate, as references are by OID.
    FOREACH old IN ARRAY ARRAY['oblako_ml', 'redshift_compat'] LOOP
        CONTINUE WHEN to_regnamespace(old) IS NULL;
        CREATE SCHEMA IF NOT EXISTS pg_oblako;
        GRANT USAGE ON SCHEMA pg_oblako TO PUBLIC;
        FOR obj IN
            SELECT c.relname AS name FROM pg_class c
            WHERE c.relnamespace = to_regnamespace(old) AND c.relkind = 'r'
        LOOP
            EXECUTE format('ALTER TABLE %I.%I SET SCHEMA pg_oblako', old, obj.name);
        END LOOP;
        FOR obj IN
            SELECT p.oid::regprocedure::text AS sig, p.prokind AS kind FROM pg_proc p
            WHERE p.pronamespace = to_regnamespace(old)
            ORDER BY p.prokind = 'a' DESC  -- aggregates first, then their support functions
        LOOP
            EXECUTE format('ALTER %s %s SET SCHEMA pg_oblako',
                           CASE obj.kind WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END, obj.sig);
        END LOOP;
        EXECUTE format('DROP SCHEMA %I', old);  -- empty now; fails rather than drop anything
    END LOOP;

    -- pg_catalog.like_escape(text, text) is PostgreSQL's own and returns the same
    -- result; oblako's copy in public was never reachable unqualified.
    DROP FUNCTION IF EXISTS public.like_escape(text, text);

    IF to_regtype('public.super') IS NOT NULL AND to_regtype('pg_catalog.super') IS NULL THEN
        ALTER DOMAIN public.super SET SCHEMA pg_catalog;
    END IF;

    FOREACH rel IN ARRAY ARRAY[
        'stl_scan', 'stv_blocklist', 'stv_tbl_perm'
    ] LOOP
        IF to_regclass('public.' || rel) IS NOT NULL THEN
            IF to_regclass('pg_catalog.' || rel) IS NULL THEN
                EXECUTE format('ALTER TABLE public.%I SET SCHEMA pg_catalog', rel);
            ELSE
                EXECUTE format('DROP TABLE public.%I', rel);  -- a stale copy
            END IF;
        END IF;
    END LOOP;

    FOREACH rel IN ARRAY ARRAY[
        'pg_table_def', 'svv_columns', 'svv_external_columns', 'svv_external_schemas',
        'svv_external_tables', 'svv_ml_model_info', 'svv_table_info', 'svv_tables'
    ] LOOP
        IF to_regclass('public.' || rel) IS NOT NULL THEN
            IF to_regclass('pg_catalog.' || rel) IS NULL THEN
                EXECUTE format('ALTER VIEW public.%I SET SCHEMA pg_catalog', rel);
            ELSE
                EXECUTE format('DROP VIEW public.%I', rel);  -- a stale copy
            END IF;
        END IF;
    END LOOP;

    -- Each object moves unless pg_catalog already has it; then the copy in public
    -- is a stale one (re-created by an older image's script) and is dropped. A
    -- drop that something depends on fails, and the entrypoint reports it.
    --
    -- Functions by exact signature, so a user's function of the same name with
    -- other arguments stays where it is. Types are written schema-free: super
    -- resolves to whichever schema holds it by now.
    FOREACH fn IN ARRAY ARRAY[
        '_final_median(numeric[])',
        '_redshift_datepart(text)',
        'add_months(timestamp without time zone, integer)',
        'convert_timezone(text, text, timestamp without time zone)',
        'convert_timezone(text, timestamp without time zone)',
        'dateadd(text, integer, timestamp without time zone)',
        'datediff(text, timestamp without time zone, timestamp without time zone)',
        'decode(integer, integer, integer, integer)',
        'format_encoding(integer)',
        'getdate()',
        'is_valid_json(text)',
        'is_valid_json_array(text)',
        'json_array_length(text)',
        'json_extract_array_element_text(text, integer)',
        'json_extract_path_text(text, text[])',
        'json_parse(text)',
        'json_serialize(super)',
        'json_serialize(text)',
        'json_typeof(super)',
        'json_typeof(text)',
        'last_day(timestamp without time zone)',
        'months_between(timestamp without time zone, timestamp without time zone)',
        'oblako_copy_from_s3(text, text, text[], text, text)',
        'oblako_ml_create_model(text)',
        'oblako_ml_drop_model(text, boolean)',
        'oblako_ml_show_model(text)',
        'oblako_ml_show_models()',
        'oblako_node_rows(regclass)',
        'oblako_unload_to_s3(text, text, text, text)',
        'pg_get_all_external_schemas()',
        'pg_get_late_binding_view_cols()',
        'pg_get_shared_redshift_schemas()',
        'redshift_acl(aclitem[], text)',
        'sysdate()',
        'trunc(timestamp without time zone)'
    ] LOOP
        IF to_regprocedure('public.' || fn) IS NOT NULL THEN
            IF to_regprocedure('pg_catalog.' || fn) IS NULL THEN
                EXECUTE format('ALTER FUNCTION public.%s SET SCHEMA pg_catalog', fn);
            ELSE
                EXECUTE format('DROP FUNCTION public.%s', fn);  -- a stale copy
            END IF;
        END IF;
    END LOOP;

    -- On a Citus cluster, forget the objects Citus recorded when older scripts
    -- created them with propagation on: it would replay them onto each worker it
    -- adds, and a worker refuses pg_catalog and pg_ schemas from a replay. Each node
    -- runs these scripts itself.
    IF to_regclass('pg_catalog.pg_dist_object') IS NOT NULL THEN
        EXECUTE $q$
            DELETE FROM pg_catalog.pg_dist_object d
            WHERE (d.classid = 'pg_namespace'::regclass
                   AND d.objid = to_regnamespace('pg_oblako'))
               OR (d.classid = 'pg_proc'::regclass AND d.objid IN (
                   SELECT oid FROM pg_proc
                   WHERE pronamespace IN ('pg_catalog'::regnamespace,
                                          to_regnamespace('pg_oblako'))))
               OR (d.classid = 'pg_class'::regclass AND d.objid IN (
                   SELECT oid FROM pg_class
                   WHERE relnamespace IN ('pg_catalog'::regnamespace,
                                          to_regnamespace('pg_oblako'))))
               OR (d.classid = 'pg_type'::regclass AND d.objid IN (
                   SELECT oid FROM pg_type WHERE typnamespace = 'pg_catalog'::regnamespace))
        $q$;
    END IF;

    -- median is an aggregate, which ALTER FUNCTION does not move
    IF to_regprocedure('public.median(numeric)') IS NOT NULL
       AND to_regprocedure('pg_catalog.median(numeric)') IS NULL THEN
        ALTER AGGREGATE public.median(numeric) SET SCHEMA pg_catalog;
    END IF;
END
$migrate$;

RESET allow_system_table_mods;
RESET citus.enable_ddl_propagation;
