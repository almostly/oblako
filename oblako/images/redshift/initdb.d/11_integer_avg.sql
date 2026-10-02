-- Redshift's AVG of an integer column returns BIGINT; PostgreSQL's returns NUMERIC.
-- So `SELECT avg(load_ms)` over an INTEGER column gives 2095 on Redshift and
-- 2095.0000000000000000 here. The return type depends on the argument's type,
-- which the wire proxy can't see, so the proxy rewrites every unqualified avg( to
-- redshift_compat.avg( (proxy/integer_avg.py) and the engine picks the overload:
--
--   SMALLINT, INTEGER, BIGINT  -> BIGINT: the sum divided by the count in integer
--                                 arithmetic (truncated toward zero), as Redshift
--   every other type           -> an exact copy of PostgreSQL's own avg, built from
--                                 its pg_aggregate entry, so nothing else changes
--
-- The schema is not on the search_path, so it never becomes the target of an
-- unqualified CREATE. Idempotent: entrypoint.sh re-applies it to every database on
-- each start, which also reaches databases created before this file existed.

CREATE SCHEMA IF NOT EXISTS redshift_compat;
GRANT USAGE ON SCHEMA redshift_compat TO PUBLIC;

CREATE OR REPLACE FUNCTION redshift_compat._avg_int_final(state int8[])
RETURNS bigint IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT CASE WHEN state[1] = 0 THEN NULL ELSE state[2] / state[1] END;
$$ LANGUAGE sql;

-- BIGINT sums can overflow int8, so their state is numeric {count, sum}.
CREATE OR REPLACE FUNCTION redshift_compat._avg_int8_accum(state numeric[], v int8)
RETURNS numeric[] IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT ARRAY[state[1] + 1, state[2] + v];
$$ LANGUAGE sql;

CREATE OR REPLACE FUNCTION redshift_compat._avg_int8_combine(a numeric[], b numeric[])
RETURNS numeric[] IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT ARRAY[a[1] + b[1], a[2] + b[2]];
$$ LANGUAGE sql;

CREATE OR REPLACE FUNCTION redshift_compat._avg_int8_final(state numeric[])
RETURNS bigint IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT CASE WHEN state[1] = 0 THEN NULL ELSE trunc(state[2] / state[1])::bigint END;
$$ LANGUAGE sql;

-- SMALLINT and INTEGER reuse PostgreSQL's own accumulators ({count, sum} as int8[]),
-- so they are as fast as the built-ins and run in parallel.
CREATE OR REPLACE AGGREGATE redshift_compat.avg(smallint) (
    SFUNC = int2_avg_accum,
    STYPE = int8[],
    COMBINEFUNC = int4_avg_combine,
    FINALFUNC = redshift_compat._avg_int_final,
    INITCOND = '{0,0}',
    PARALLEL = SAFE
);

CREATE OR REPLACE AGGREGATE redshift_compat.avg(integer) (
    SFUNC = int4_avg_accum,
    STYPE = int8[],
    COMBINEFUNC = int4_avg_combine,
    FINALFUNC = redshift_compat._avg_int_final,
    INITCOND = '{0,0}',
    PARALLEL = SAFE
);

CREATE OR REPLACE AGGREGATE redshift_compat.avg(bigint) (
    SFUNC = redshift_compat._avg_int8_accum,
    STYPE = numeric[],
    COMBINEFUNC = redshift_compat._avg_int8_combine,
    FINALFUNC = redshift_compat._avg_int8_final,
    INITCOND = '{0,0}',
    PARALLEL = SAFE
);

-- Every other avg: mirror PostgreSQL's definition component by component.
DO $mirror$
DECLARE
    a record;
    opts text;
BEGIN
    FOR a IN
        SELECT p.proargtypes[0]::regtype AS argtype,
               g.aggtransfn, g.aggfinalfn, g.aggcombinefn, g.aggserialfn,
               g.aggdeserialfn, g.aggtranstype::regtype AS stype, g.agginitval,
               p.proparallel
        FROM pg_aggregate g
        JOIN pg_proc p ON p.oid = g.aggfnoid
        WHERE p.proname = 'avg'
          AND p.pronamespace = 'pg_catalog'::regnamespace
          AND p.proargtypes[0] NOT IN ('int2'::regtype, 'int4'::regtype, 'int8'::regtype)
    LOOP
        opts := format('SFUNC = %s, STYPE = %s', a.aggtransfn::regproc, a.stype);
        IF a.aggfinalfn <> 0 THEN
            opts := opts || format(', FINALFUNC = %s', a.aggfinalfn::regproc);
        END IF;
        IF a.aggcombinefn <> 0 THEN
            opts := opts || format(', COMBINEFUNC = %s', a.aggcombinefn::regproc);
        END IF;
        IF a.aggserialfn <> 0 THEN
            opts := opts || format(', SERIALFUNC = %s, DESERIALFUNC = %s',
                                   a.aggserialfn::regproc, a.aggdeserialfn::regproc);
        END IF;
        IF a.agginitval IS NOT NULL THEN
            opts := opts || format(', INITCOND = %L', a.agginitval);
        END IF;
        IF a.proparallel = 's' THEN
            opts := opts || ', PARALLEL = SAFE';
        END IF;
        EXECUTE format('CREATE OR REPLACE AGGREGATE redshift_compat.avg(%s) (%s)',
                       a.argtype, opts);
    END LOOP;
END
$mirror$;
