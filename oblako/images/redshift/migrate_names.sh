#!/usr/bin/env bash
# Move a data directory made with PostgreSQL's 63-byte names onto this image's
# 127-byte build. PostgreSQL won't start on a data directory initialized with a
# different NAMEDATALEN, and pg_upgrade refuses it too, so the move is a dump and
# a restore, in two steps the entrypoint runs:
#
#   dump     before the stock entrypoint: if the data directory has 63-byte names,
#            the stock server (still in /usr/lib/postgresql/16) dumps the roles and
#            each database's schemas, pg_oblako included (pg_dump skips pg_*
#            schemas unless named), and the ALTER/DROP grants and database grants
#            by name; the old files move aside, so the stock entrypoint initializes
#            a fresh data directory, init scripts and all.
#   restore  once the new server is up, before the proxy starts: the dumps go back
#            in, pg_oblako rows that hold OIDs are re-resolved by name, and the
#            dumps are kept in the data directory (oblako-names64-backup/). The old
#            files are deleted only when every table came back.
#
# Each step leaves a marker, so a container restarted halfway resumes. The Citus
# variant isn't migrated: its tables are sharded across nodes.
set -euo pipefail

STOCK=/usr/lib/postgresql/16/bin
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
WORK=/var/lib/postgresql/oblako-names64
PG_USER="${POSTGRES_USER:-postgres}"
STOCK_PORT=5499

as_postgres() {
  if [ "$(id -u)" = 0 ]; then gosu postgres "$@"; else "$@"; fi
}

has_63_byte_names() {
  [ -s "$PGDATA/PG_VERSION" ] &&
    "$STOCK/pg_controldata" "$PGDATA" 2>/dev/null |
    grep -q '^Maximum length of identifiers: *64$'
}

quote_literal() {
  printf "'%s'" "${1//\'/\'\'}"
}

stock_psql() {
  "$STOCK/psql" -h "$WORK" -p "$STOCK_PORT" -U "$PG_USER" -X -v ON_ERROR_STOP=1 "$@"
}

dump() {
  has_63_byte_names || return 0
  if [ "${OBLAKO_CITUS:-0}" = "1" ]; then
    echo "oblako: this data directory was made with 63-byte names; the cluster" \
      "variant can't move it to 127 (its tables are sharded). Recreate the" \
      "cluster's volumes to start fresh." >&2
    exit 1
  fi
  echo "oblako: moving the data directory to Redshift's 127-byte names (once)"
  rm -rf "$WORK"
  mkdir -p "$WORK"
  chown postgres:postgres "$WORK"
  as_postgres "$STOCK/pg_ctl" -D "$PGDATA" -w -l "$WORK/stock.log" -o \
    "-p $STOCK_PORT -c listen_addresses='' -c unix_socket_directories='$WORK' -c shared_preload_libraries=''" \
    start >/dev/null
  as_postgres "$STOCK/pg_dumpall" -h "$WORK" -p "$STOCK_PORT" -U "$PG_USER" \
    --globals-only -f "$WORK/globals.sql"
  stock_psql -d postgres -Atc "SELECT datname FROM pg_database
      WHERE datallowconn AND datname NOT IN ('template0', 'template1') ORDER BY 1" \
    >"$WORK/databases"
  local i=0 db
  while IFS= read -r db; do
    i=$((i + 1))
    stock_psql -d postgres -Atc "SELECT pg_get_userbyid(datdba) FROM pg_database
        WHERE datname = $(quote_literal "$db")" >"$WORK/db$i.owner"
    # the database's grants, as GRANT statements (PUBLIC's defaults replaced)
    stock_psql -d postgres -Atc "SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC;', datname)
        FROM pg_database WHERE datname = $(quote_literal "$db") AND datacl IS NOT NULL
        UNION ALL
        SELECT format('GRANT %s ON DATABASE %I TO %s%s;', a.privilege_type, d.datname,
                      CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END,
                      CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END)
        FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) a
        WHERE d.datname = $(quote_literal "$db") AND a.grantee <> d.datdba" \
      >"$WORK/db$i.grants.sql"
    # its own schemas and pg_oblako, which pg_dump leaves out unless named
    local schemas=()
    while IFS= read -r ns; do
      schemas+=(-n "\"${ns//\"/\"\"}\"")
    done < <(stock_psql -d "$db" -Atc "SELECT nspname FROM pg_namespace
        WHERE (nspname !~ '^pg_' AND nspname <> 'information_schema')
           OR nspname = 'pg_oblako'")
    as_postgres "$STOCK/pg_dump" -h "$WORK" -p "$STOCK_PORT" -U "$PG_USER" -d "$db" \
      -Fc "${schemas[@]}" -f "$WORK/db$i.dump"
    # ALTER and DROP grants hold OIDs; keep them by name
    stock_psql -d "$db" -Atc "SELECT CASE WHEN to_regclass('pg_oblako.object_privileges') IS NULL THEN ''
        ELSE (SELECT string_agg(format(
            'SELECT pg_oblako.object_privilege(true, ARRAY[%L], %L, ARRAY[%L], ARRAY[%L]::text[], %L);',
            p.privilege, p.objkind,
            CASE p.objkind
                WHEN 'relation' THEN (SELECT format('%I.%I', n.nspname, c.relname)
                                        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                                       WHERE c.oid = p.objid)
                ELSE (SELECT quote_ident(nspname) FROM pg_namespace WHERE oid = p.objid) END,
            CASE p.grantee WHEN 0 THEN NULL ELSE pg_get_userbyid(p.grantee) END,
            p.admin_option), E'\n')
          FROM pg_oblako.object_privileges p) END" >"$WORK/db$i.privileges.sql"
    stock_psql -d "$db" -Atc "SELECT count(*) FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'v', 'm')
          AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'" \
      >"$WORK/db$i.relations"
  done <"$WORK/databases"
  as_postgres "$STOCK/pg_ctl" -D "$PGDATA" -w -m fast stop >/dev/null
  mkdir -p "$WORK/old-data"
  find "$PGDATA" -mindepth 1 -maxdepth 1 -exec mv -t "$WORK/old-data" {} +
  chown -R postgres:postgres "$WORK"
  touch "$WORK/dumped"
}

new_psql() {
  psql -U "$PG_USER" -p "${OBLAKO_PG_PORT:-5433}" -X -q "$@"
}

restore() {
  [ -f "$WORK/dumped" ] || return 0
  export PGOPTIONS="-c log_min_messages=fatal"
  if [ -f "$WORK/restoring" ]; then
    # a restore that stopped halfway; running it again would load rows twice
    echo "oblako: an earlier move to 127-byte names stopped halfway; the old data" \
      "directory is in $WORK/old-data (in this container), the dumps in $WORK" >&2
    return 0
  fi
  touch "$WORK/restoring"
  new_psql -d postgres -f "$WORK/globals.sql" >/dev/null 2>&1 || true
  local i=0 db failed=0
  while IFS= read -r db; do
    i=$((i + 1))
    if [ -z "$(new_psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = $(quote_literal "$db")")" ]; then
      new_psql -d postgres -c "CREATE DATABASE \"${db//\"/\"\"}\" OWNER \"$(sed 's/"/""/g' "$WORK/db$i.owner")\""
    fi
    # objects the fresh init already made report "already exists"; the rest,
    # and every table's rows, come back. As a replica, so foreign keys the fresh
    # init made don't refuse rows that arrive before the ones they point to, and
    # quiet in the server log, where those expected errors would only alarm.
    PGOPTIONS="-c session_replication_role=replica -c log_min_messages=fatal" \
      pg_restore -U "$PG_USER" -p "${OBLAKO_PG_PORT:-5433}" -d "$db" "$WORK/db$i.dump" \
      >"$WORK/db$i.restore.log" 2>&1 || true
    new_psql -d "$db" >/dev/null 2>&1 <<SQL || true
DO \$fix\$ BEGIN
    IF to_regclass('pg_oblako.ddm_attachments') IS NOT NULL THEN
        UPDATE pg_oblako.ddm_attachments a
           SET relid = coalesce(to_regclass(format('%I.%I', a.schema_name, a.table_name)), a.relid);
    END IF;
    IF to_regclass('pg_oblako.object_privileges') IS NOT NULL THEN
        TRUNCATE pg_oblako.object_privileges;
    END IF;
END \$fix\$;
SQL
    new_psql -d "$db" -f "$WORK/db$i.privileges.sql" >/dev/null 2>&1 || true
    new_psql -d postgres -f "$WORK/db$i.grants.sql" >/dev/null 2>&1 || true
    local now
    now=$(new_psql -d "$db" -Atc "SELECT count(*) FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'v', 'm')
          AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'")
    if [ "$now" -lt "$(cat "$WORK/db$i.relations")" ]; then
      echo "oblako: $db came back with $now of $(cat "$WORK/db$i.relations") tables and views;" \
        "see $WORK/db$i.restore.log" >&2
      failed=1
    fi
  done <"$WORK/databases"
  local backup="$PGDATA/oblako-names64-backup"
  mkdir -p "$backup"
  cp "$WORK"/*.sql "$WORK"/*.dump "$WORK"/databases "$WORK"/*.log "$backup"/ 2>/dev/null || true
  chown -R postgres:postgres "$backup"
  if [ "$failed" = 0 ]; then
    rm -rf "$WORK"
    echo "oblako: the data directory now has 127-byte names; the dumps are in $backup"
  else
    rm -f "$WORK/dumped" "$WORK/restoring"
    echo "oblako: the move to 127-byte names left some tables behind; the old data" \
      "directory is in $WORK/old-data (in this container) and the dumps in $backup" >&2
  fi
}

case "${1:-}" in
  dump) dump ;;
  restore) restore ;;
  *) echo "usage: $0 dump|restore" >&2; exit 2 ;;
esac
