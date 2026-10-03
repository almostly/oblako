#!/usr/bin/env bash
# redshift-local MPP entrypoint (Citus). Same Redshift-compatible engine as the
# single-node image; the role is set by OBLAKO_CITUS_ROLE:
#   coordinator (default) - clients connect here (Redshift proxy on 5439); it
#                           registers the workers named in OBLAKO_CITUS_WORKERS
#                           (comma-separated hostnames, each an internal node).
#   worker                - an internal shard node the coordinator distributes onto.
# Nodes authenticate to each other with the master user's password, which the
# coordinator stores in Citus's pg_dist_authinfo (nodeid 0 = every node), so the
# cluster keeps password authentication on every node. Everything else (PG on the
# internal port + the wire proxy) is delegated to the single-node entrypoint.
set -e

export OBLAKO_PG_PORT="${OBLAKO_PG_PORT:-5433}"
ROLE="${OBLAKO_CITUS_ROLE:-coordinator}"

if [ "$ROLE" = "coordinator" ]; then
  (
    # Once our own PostgreSQL is accepting connections, register this coordinator
    # and each worker with Citus. Idempotent: citus_add_node no-ops if present.
    until pg_isready -h 127.0.0.1 -p "$OBLAKO_PG_PORT" -q 2>/dev/null; do sleep 0.5; done
    DB="${POSTGRES_DB:-oblako}"
    USER="${POSTGRES_USER:-oblako}"
    run() {
      PGPASSWORD="${POSTGRES_PASSWORD:-}" psql -v ON_ERROR_STOP=0 -h 127.0.0.1 -p "$OBLAKO_PG_PORT" -U "$USER" -d "$DB" -tAc "$1" 2>/dev/null
    }
    if [ -n "${POSTGRES_PASSWORD:-}" ]; then
      run "INSERT INTO pg_dist_authinfo (nodeid, rolename, authinfo)
           VALUES (0, '${USER}', 'password=${POSTGRES_PASSWORD}')
           ON CONFLICT DO NOTHING;"
    fi
    run "SELECT citus_set_coordinator_host('${OBLAKO_CITUS_COORDINATOR_HOST:-redshift-coordinator}', ${OBLAKO_PG_PORT});"
    if [ -n "${OBLAKO_CITUS_WORKERS:-}" ]; then
      IFS=','
      for w in ${OBLAKO_CITUS_WORKERS}; do
        until pg_isready -h "$w" -p "$OBLAKO_PG_PORT" -q 2>/dev/null; do sleep 0.5; done
        until run "SELECT citus_add_node('$w', ${OBLAKO_PG_PORT});" >/dev/null; do sleep 1; done
      done
      echo "oblako: registered Citus workers: ${OBLAKO_CITUS_WORKERS}"
    fi
  ) &
fi

# Hand off to the single-node entrypoint: PostgreSQL (with citus + oblako_redshift
# preloaded) on the internal port, and the Redshift wire proxy on 5439.
exec /usr/local/bin/oblako-entrypoint.sh "$@"
