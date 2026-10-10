-- Install the Redshift catalog views into every database a client can reach, not
-- just POSTGRES_DB. Tools that manage a cluster rather than one database walk
-- pg_database and reconnect per entry, so a database without the compat layer
-- fails on the first Redshift-only view or function.
--
--   template1  so databases created later (e.g. Redshift's conventional `dev`)
--              inherit them.
--   postgres   initdb creates it before these scripts run, so template1 does not
--              reach it; it has to be seeded directly.
--
-- Pure SQL views and functions, and 05 is idempotent (CREATE OR REPLACE), so
-- re-applying it per database is safe.
\c template1
\i /docker-entrypoint-initdb.d/05_catalog_views.sql
\c postgres
\i /docker-entrypoint-initdb.d/05_catalog_views.sql
