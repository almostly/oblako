-- Amazon Redshift date/time functions that plain PostgreSQL 16 doesn't ship:
-- GETDATE, SYSDATE, DATEADD, DATEDIFF, ADD_MONTHS, LAST_DAY, MONTHS_BETWEEN,
-- TRUNC(timestamp), CONVERT_TIMEZONE. Implemented as thin SQL/plpgsql wrappers
-- over PostgreSQL, faithful to Redshift semantics (notably DATEDIFF counts
-- datepart *boundaries crossed*, not elapsed time). Pure SQL/plpgsql (no
-- plpython) so they can also seed template1 for databases created later.
--
-- Datepart is a quoted string here, e.g. DATEADD('day', 7, ts). Redshift also
-- accepts the bare keyword form DATEADD(day, 7, ts), which PostgreSQL would parse
-- as a column; the wire proxy quotes it (proxy/datepart.py), so both forms work.

-- Normalize Redshift datepart names/abbreviations to a canonical unit.
-- Note: per Redshift, 'm' is MINUTE (month is 'mon'/'mons'/'mm').
CREATE OR REPLACE FUNCTION _redshift_datepart(p text)
RETURNS text IMMUTABLE LANGUAGE sql AS $$
    SELECT CASE lower(p)
        WHEN 'y' THEN 'year' WHEN 'yr' THEN 'year' WHEN 'yrs' THEN 'year'
        WHEN 'year' THEN 'year' WHEN 'years' THEN 'year'
        WHEN 'q' THEN 'quarter' WHEN 'qtr' THEN 'quarter'
        WHEN 'quarter' THEN 'quarter' WHEN 'quarters' THEN 'quarter'
        WHEN 'mm' THEN 'month' WHEN 'mon' THEN 'month' WHEN 'mons' THEN 'month'
        WHEN 'month' THEN 'month' WHEN 'months' THEN 'month'
        WHEN 'w' THEN 'week' WHEN 'wk' THEN 'week' WHEN 'weeks' THEN 'week'
        WHEN 'week' THEN 'week'
        WHEN 'd' THEN 'day' WHEN 'day' THEN 'day' WHEN 'days' THEN 'day'
        WHEN 'doy' THEN 'day' WHEN 'dayofyear' THEN 'day'
        WHEN 'h' THEN 'hour' WHEN 'hr' THEN 'hour' WHEN 'hrs' THEN 'hour'
        WHEN 'hour' THEN 'hour' WHEN 'hours' THEN 'hour'
        WHEN 'm' THEN 'minute' WHEN 'min' THEN 'minute' WHEN 'mins' THEN 'minute'
        WHEN 'minute' THEN 'minute' WHEN 'minutes' THEN 'minute'
        WHEN 's' THEN 'second' WHEN 'sec' THEN 'second' WHEN 'secs' THEN 'second'
        WHEN 'second' THEN 'second' WHEN 'seconds' THEN 'second'
        WHEN 'ms' THEN 'millisecond' WHEN 'millisecond' THEN 'millisecond'
        WHEN 'milliseconds' THEN 'millisecond'
        WHEN 'us' THEN 'microsecond' WHEN 'microsecond' THEN 'microsecond'
        WHEN 'microseconds' THEN 'microsecond'
        ELSE lower(p)
    END
$$;

-- GETDATE() / SYSDATE() — current statement timestamp (no time zone), like Redshift.
CREATE OR REPLACE FUNCTION getdate()
RETURNS timestamp LANGUAGE sql AS $$ SELECT now()::timestamp $$;

CREATE OR REPLACE FUNCTION sysdate()
RETURNS timestamp LANGUAGE sql AS $$ SELECT now()::timestamp $$;

-- ADD_MONTHS(date|timestamp, n) -> shifts by n months.
CREATE OR REPLACE FUNCTION add_months(d timestamp, n int)
RETURNS timestamp IMMUTABLE LANGUAGE sql AS $$
    SELECT d + (n || ' months')::interval
$$;

-- LAST_DAY(date|timestamp) -> the last day of that month, as a date.
CREATE OR REPLACE FUNCTION last_day(d timestamp)
RETURNS date IMMUTABLE LANGUAGE sql AS $$
    SELECT (date_trunc('month', d) + interval '1 month - 1 day')::date
$$;

-- MONTHS_BETWEEN(d1, d2) -> fractional months from d2 to d1 (Oracle/Redshift style).
CREATE OR REPLACE FUNCTION months_between(d1 timestamp, d2 timestamp)
RETURNS double precision IMMUTABLE LANGUAGE sql AS $$
    SELECT (extract(year FROM d1) - extract(year FROM d2)) * 12
         + (extract(month FROM d1) - extract(month FROM d2))
         + (extract(day FROM d1) - extract(day FROM d2)) / 31.0
$$;

-- TRUNC(timestamp) -> date (drops the time), like Redshift. (PostgreSQL's
-- built-in trunc() is numeric-only, so this just adds a timestamp overload.)
CREATE OR REPLACE FUNCTION trunc(t timestamp)
RETURNS date IMMUTABLE LANGUAGE sql AS $$ SELECT t::date $$;

-- DATEADD(datepart, n, ts) -> ts shifted by n dateparts (returns timestamp).
CREATE OR REPLACE FUNCTION dateadd(datepart text, n int, ts timestamp)
RETURNS timestamp IMMUTABLE LANGUAGE plpgsql AS $$
DECLARE p text := _redshift_datepart(datepart);
BEGIN
    -- PostgreSQL intervals have no 'quarter', so expand it to months.
    IF p = 'quarter' THEN
        RETURN ts + ((n * 3) || ' months')::interval;
    END IF;
    RETURN ts + (n || ' ' || p)::interval;
END
$$;

-- DATEDIFF(datepart, start, end) -> number of datepart BOUNDARIES crossed,
-- matching Redshift (e.g. DATEDIFF('year','2020-12-31','2021-01-01') = 1).
CREATE OR REPLACE FUNCTION datediff(datepart text, a timestamp, b timestamp)
RETURNS bigint IMMUTABLE LANGUAGE plpgsql AS $$
DECLARE p text := _redshift_datepart(datepart);
BEGIN
    RETURN CASE p
        WHEN 'year' THEN (extract(year FROM b) - extract(year FROM a))::bigint
        WHEN 'quarter' THEN
            ((extract(year FROM b) * 4 + floor((extract(month FROM b) - 1) / 3))
           - (extract(year FROM a) * 4 + floor((extract(month FROM a) - 1) / 3)))::bigint
        WHEN 'month' THEN
            ((extract(year FROM b) * 12 + extract(month FROM b))
           - (extract(year FROM a) * 12 + extract(month FROM a)))::bigint
        WHEN 'week' THEN
            floor(extract(epoch FROM (date_trunc('week', b) - date_trunc('week', a))) / 604800)::bigint
        WHEN 'day' THEN (b::date - a::date)::bigint
        WHEN 'hour' THEN
            floor(extract(epoch FROM (date_trunc('hour', b) - date_trunc('hour', a))) / 3600)::bigint
        WHEN 'minute' THEN
            floor(extract(epoch FROM (date_trunc('minute', b) - date_trunc('minute', a))) / 60)::bigint
        WHEN 'second' THEN
            floor(extract(epoch FROM (date_trunc('second', b) - date_trunc('second', a))))::bigint
        ELSE NULL
    END;
END
$$;

-- CONVERT_TIMEZONE(source, target, ts) and CONVERT_TIMEZONE(target, ts)
-- (2-arg assumes the input is UTC), like Redshift.
CREATE OR REPLACE FUNCTION convert_timezone(source_tz text, target_tz text, ts timestamp)
RETURNS timestamp IMMUTABLE LANGUAGE sql AS $$
    SELECT (ts AT TIME ZONE source_tz) AT TIME ZONE target_tz
$$;

CREATE OR REPLACE FUNCTION convert_timezone(target_tz text, ts timestamp)
RETURNS timestamp IMMUTABLE LANGUAGE sql AS $$
    SELECT (ts AT TIME ZONE 'UTC') AT TIME ZONE target_tz
$$;
