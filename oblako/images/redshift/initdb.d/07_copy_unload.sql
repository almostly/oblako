-- Redshift COPY/UNLOAD <-> S3 bridge (plpython3u).
--
-- The wire proxy rewrites `COPY t FROM 's3://...'` and `UNLOAD ('q') TO 's3://...'`
-- into calls to these functions (see proxy/copy_unload.py). They do the object
-- store work via boto3 + pyarrow, running SQL in-session through SPI (plpy), so a
-- temp table created earlier in the same batch is visible to UNLOAD. The heavy
-- lifting and type handling live in the importable `copy_unload` module (on the
-- server's PYTHONPATH), keeping these wrappers thin and shared with the proxy.

CREATE EXTENSION IF NOT EXISTS plpython3u;

-- UNLOAD ('query') TO 's3://prefix/' [FORMAT AS PARQUET|CSV]: run the query and
-- write the result to S3 (Parquet, CSV, or default pipe-delimited text). `opts` is
-- a JSON blob of delimiter/header/null_as/quote. Returns the number of rows.
CREATE OR REPLACE FUNCTION oblako_unload_to_s3(query text, uri text, fmt text DEFAULT 'PARQUET', opts text DEFAULT '{}')
RETURNS bigint AS $$
import copy_unload
return copy_unload.do_unload(plpy, query, uri, fmt, opts)
$$ LANGUAGE plpython3u;

-- COPY table [(cols)] FROM 's3://prefix' [FORMAT AS PARQUET|CSV]: read the object(s)
-- under the prefix and insert them (Parquet by column name; CSV/text by position).
-- `opts` is a JSON blob of delimiter/ignore_header/null_as/quote. Returns row count.
CREATE OR REPLACE FUNCTION oblako_copy_from_s3(tbl text, uri text, cols text[] DEFAULT NULL, fmt text DEFAULT 'PARQUET', opts text DEFAULT '{}')
RETURNS bigint AS $$
import copy_unload
return copy_unload.do_copy(plpy, tbl, uri, cols, fmt, opts)
$$ LANGUAGE plpython3u;
