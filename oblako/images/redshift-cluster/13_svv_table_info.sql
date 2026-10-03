-- SVV_TABLE_INFO on a cluster, from what Citus actually did with each table:
-- DISTKEY(col) tables are hash-distributed (diststyle KEY(col)), DISTSTYLE ALL
-- tables are reference tables copied to every node (ALL), and the rest stay on
-- the leader (EVEN). For KEY tables, tbl_rows counts rows across the compute
-- nodes and skew_rows is the ratio of rows on the fullest node to rows on the
-- emptiest (Redshift computes it per slice; here a compute node is the unit).
-- Replaces the single-node definition (05_catalog_views.sql), keeping its columns
-- and order and adding skew_rows, as Redshift has it.

CREATE OR REPLACE FUNCTION oblako_node_rows(t regclass)
RETURNS TABLE (node text, rows bigint)
LANGUAGE sql STABLE AS $$
    SELECT s.nodename::text, sum(r.result::bigint)
    FROM run_command_on_shards(t, 'SELECT count(*) FROM %s') r
    JOIN citus_shards s ON s.shardid = r.shardid
    WHERE r.success
    GROUP BY 1
$$;

CREATE OR REPLACE VIEW svv_table_info AS
WITH dist AS (
    SELECT logicalrelid,
           partmethod,
           column_to_column_name(logicalrelid, partkey) AS distkey
    FROM pg_dist_partition
),
key_rows AS (
    -- KEY: rows summed over the compute nodes; ALL: the rows of one copy
    SELECT d.logicalrelid,
           CASE WHEN d.partmethod = 'h' THEN sum(n.rows) ELSE max(n.rows) END::bigint
               AS total,
           CASE WHEN d.partmethod = 'h' AND min(n.rows) > 0
                THEN round(max(n.rows)::numeric / min(n.rows), 2) END AS skew
    FROM dist d, LATERAL oblako_node_rows(d.logicalrelid) n
    WHERE d.partmethod IN ('h', 'n')
    GROUP BY d.logicalrelid, d.partmethod
)
SELECT
    current_database()                AS database,
    n.nspname                         AS schema,
    c.oid::int                        AS table_id,
    c.relname                         AS "table",
    CASE d.partmethod
        WHEN 'h' THEN 'KEY(' || d.distkey || ')'
        WHEN 'n' THEN 'ALL'
        ELSE 'EVEN'
    END::text                         AS diststyle,
    (SELECT a.attname::text COLLATE "default"
     FROM pg_index i
     JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
     WHERE i.indrelid = c.oid AND NOT i.indisprimary
     ORDER BY i.indexrelid LIMIT 1)   AS sortkey1,
    0::int                            AS size,
    0::numeric                        AS pct_used,
    0::numeric                        AS unsorted,
    0::numeric                        AS stats_off,
    COALESCE(k.total, GREATEST(c.reltuples, 0)::bigint) AS tbl_rows,
    COALESCE(k.total, GREATEST(c.reltuples, 0)::bigint) AS estimated_visible_rows,
    k.skew                            AS skew_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN dist d ON d.logicalrelid = c.oid
LEFT JOIN key_rows k ON k.logicalrelid = c.oid
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast', 'citus')
  AND NOT c.relname ~ '_[0-9]+$';  -- shard tables on the leader, if any
