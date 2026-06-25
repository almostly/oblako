-- Also install the Redshift date/time functions into template1 so any database
-- created later (e.g. Redshift's conventional `dev`) inherits them. They are
-- pure SQL/plpgsql, so no extension is needed in template1.
\c template1
\i /docker-entrypoint-initdb.d/03_date_functions.sql
