-- Minimal Amazon Redshift system tables (enough for tooling that probes them).
-- Ported from hearthsim/pgredshift.

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
