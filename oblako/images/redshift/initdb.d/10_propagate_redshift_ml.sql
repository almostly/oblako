-- Install Redshift ML into every database a client can reach, not just
-- POSTGRES_DB (same reasoning as 06_): template1 so databases created later
-- inherit it, and postgres directly. A Redshift ML model belongs to one database.
\c template1
\i /docker-entrypoint-initdb.d/09_redshift_ml.sql
\c postgres
\i /docker-entrypoint-initdb.d/09_redshift_ml.sql
