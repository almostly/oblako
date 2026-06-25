-- Also install the Redshift catalog views into template1 so databases created
-- later (e.g. Redshift's conventional `dev`) inherit them. Pure SQL views.
\c template1
\i /docker-entrypoint-initdb.d/05_catalog_views.sql
