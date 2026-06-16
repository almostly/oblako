-- Modern PostgreSQL only ships Python 3, so oblako's Redshift uses plpython3u
-- (also what Redshift ML's generated prediction UDFs target).
CREATE EXTENSION IF NOT EXISTS plpython3u;

-- Amazon Redshift's Python UDFs use `LANGUAGE plpythonu`. (On real Redshift that
-- is Python *2*, and Redshift is sunsetting Python UDFs entirely — no new ones
-- from Patch 198, existing ones run until 2026-06-30; Lambda UDFs are the
-- forward path.) We alias `plpythonu` onto the Python 3 handler so Redshift-style
-- `CREATE FUNCTION ... LANGUAGE plpythonu` runs here — with Python 3 semantics,
-- since Python 2 is EOL and unavailable on PostgreSQL 16.
CREATE OR REPLACE LANGUAGE plpythonu
    HANDLER plpython3_call_handler
    INLINE plpython3_inline_handler
    VALIDATOR plpython3_validator;
