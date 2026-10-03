-- Citus first, in the cluster's database, before the Redshift compatibility
-- scripts (00_extensions.sql onwards) create their objects.
CREATE EXTENSION IF NOT EXISTS citus;
