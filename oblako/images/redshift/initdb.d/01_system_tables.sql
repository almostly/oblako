-- Minimal Amazon Redshift system tables (enough for tooling that probes them).
-- Ported from hearthsim/pgredshift.

-- Redshift's built-ins live in pg_catalog, so these are created there (see
-- 99_system_catalog.sql); allow_system_table_mods permits it (superuser).
SET allow_system_table_mods = on;
-- every node creates these itself; Citus must not replay them (a worker refuses
-- pg_catalog and pg_ schemas from a replay). A placeholder without Citus.
SET citus.enable_ddl_propagation = off;
SET search_path = pg_catalog, public;

CREATE TABLE IF NOT EXISTS stl_scan AS SELECT a.n FROM generate_series(1, 30) AS a(n);

CREATE TABLE IF NOT EXISTS stv_blocklist (
	slice INTEGER, col INTEGER, tbl INTEGER, blocknum INTEGER,
	num_values INTEGER, extended_limits INTEGER, minvalue BIGINT, maxvalue BIGINT,
	sb_pos INTEGER, pinned INTEGER, on_disk INTEGER, modified INTEGER,
	hdr_modified INTEGER, unsorted INTEGER, tombstone INTEGER,
	preferred_diskno INTEGER, "temporary" INTEGER, newblock INTEGER,
	num_readers INTEGER, flags INTEGER
);

CREATE TABLE IF NOT EXISTS stv_tbl_perm (
	slice INTEGER, id INTEGER, name VARCHAR(72), rows BIGINT, sorted_rows BIGINT,
	"temp" INTEGER, db_id INTEGER, insert_pristine INTEGER,
	delete_pristine INTEGER, backup INTEGER
);

-- back to the session defaults, for whoever runs this file next in the session
RESET search_path;
RESET allow_system_table_mods;
RESET citus.enable_ddl_propagation;
