#!/usr/bin/env bash
# Run PostgreSQL on an internal port and the Redshift-compat wire proxy on the
# published port, so clients transparently get Redshift-tolerant SQL (the proxy
# strips DISTSTYLE/DISTKEY/SORTKEY/ENCODE that PostgreSQL can't parse). From the
# outside it's still a single container listening on the usual port.
set -e

export OBLAKO_PG_PORT="${OBLAKO_PG_PORT:-5433}"      # PostgreSQL, internal only
export OBLAKO_PROXY_PORT="${OBLAKO_PROXY_PORT:-5432}" # what clients connect to
export OBLAKO_PG_HOST=127.0.0.1

# Start the proxy once PostgreSQL is accepting connections on the internal port.
(
  until pg_isready -h 127.0.0.1 -p "$OBLAKO_PG_PORT" -q 2>/dev/null; do
    sleep 0.5
  done
  exec python3 /usr/local/bin/redshift_proxy.py
) &

# Hand off to the stock postgres entrypoint (initdb, auth, etc.); PostgreSQL
# listens on the internal port so only the proxy fronts it.
exec docker-entrypoint.sh postgres -p "$OBLAKO_PG_PORT"
