#!/usr/bin/env bash
# Run PostgreSQL on an internal port and the Redshift-compat wire proxy on the
# published port, so clients transparently get Redshift-tolerant SQL (the proxy
# strips DISTSTYLE/DISTKEY/SORTKEY/ENCODE that PostgreSQL can't parse). From the
# outside it's still a single container listening on the usual port.
set -e

export OBLAKO_PG_PORT="${OBLAKO_PG_PORT:-5433}"       # PostgreSQL, internal only
export OBLAKO_PROXY_PORT="${OBLAKO_PROXY_PORT:-5439}" # what clients connect to (Redshift's port)
export OBLAKO_PG_HOST=127.0.0.1

# Put the copy_unload bridge module on plpython3u's import path (the oblako_*
# COPY/UNLOAD functions `import copy_unload`). It lives next to the proxy in
# /usr/local/bin; PostgreSQL inherits this env, so plpython finds it too.
export PYTHONPATH="/usr/local/bin${PYTHONPATH:+:$PYTHONPATH}"

# TLS cert for the proxy. oblako mounts this machine's cert and key at
# /etc/oblako-redshift (made once in ~/.oblako/redshift/tls, so `oblako trust`
# stays valid). Without a mount, as under plain `docker compose`, this block makes
# a cert for this container. Disable TLS entirely with OBLAKO_SSL=0.
CERT_DIR=/etc/oblako-redshift
export OBLAKO_SSL_CERT="${OBLAKO_SSL_CERT:-$CERT_DIR/server.crt}"
export OBLAKO_SSL_KEY="${OBLAKO_SSL_KEY:-$CERT_DIR/server.key}"
if [ "${OBLAKO_SSL:-1}" = "1" ] && [ ! -f "$OBLAKO_SSL_CERT" ]; then
  mkdir -p "$(dirname "$OBLAKO_SSL_CERT")"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$OBLAKO_SSL_KEY" -out "$OBLAKO_SSL_CERT" \
    -subj "/O=oblako/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
    -addext "basicConstraints=critical,CA:FALSE" -addext "extendedKeyUsage=serverAuth" \
    >/dev/null 2>&1 || echo "oblako: could not generate TLS cert; proxy will run without SSL"
fi

# Start the proxy once PostgreSQL is accepting connections on the internal port.
# First bring every database up to date: Redshift's system objects in pg_catalog
# (initdb.d/99_system_catalog.sql) and the pg_oblako AVG aggregates the
# proxy routes avg() to (initdb.d/11_integer_avg.sql). initdb scripts run only on a
# fresh volume, and these must also reach databases created before they existed.
# Both scripts are idempotent.
(
  until pg_isready -h 127.0.0.1 -p "$OBLAKO_PG_PORT" -q 2>/dev/null; do
    sleep 0.5
  done
  for db in $(psql -U "${POSTGRES_USER:-postgres}" -p "$OBLAKO_PG_PORT" -d postgres -Atq \
      -c "SELECT datname FROM pg_database WHERE datallowconn" 2>/dev/null); do
    # system objects to pg_catalog and internal schemas renamed first, so 11 finds
    # pg_oblako in place rather than creating it beside an old one
    psql -U "${POSTGRES_USER:-postgres}" -p "$OBLAKO_PG_PORT" -d "$db" -q \
      -f /docker-entrypoint-initdb.d/99_system_catalog.sql >/dev/null 2>&1 \
      || echo "oblako: could not move the Redshift system objects in $db"
    psql -U "${POSTGRES_USER:-postgres}" -p "$OBLAKO_PG_PORT" -d "$db" -q \
      -f /docker-entrypoint-initdb.d/11_integer_avg.sql >/dev/null 2>&1 \
      || echo "oblako: could not install the integer AVG overloads in $db"
  done
  exec python3 /usr/local/bin/redshift_proxy.py
) &

# Redshift ML training agent: trains queued CREATE MODELs in containers on the
# host Docker daemon. Only when the socket is mounted; without it CREATE MODEL
# fails with a clear message instead of queueing a model nothing will train.
if [ -S /var/run/docker.sock ]; then
  (
    until pg_isready -h 127.0.0.1 -p "$OBLAKO_PG_PORT" -q 2>/dev/null; do
      sleep 0.5
    done
    exec python3 /usr/local/bin/redshift_ml.py agent
  ) &
fi

# A data directory initialised before loopback TCP followed POSTGRES_HOST_AUTH_METHOD
# (initdb.d/12_password_auth.sh) gets the same change here, on every start.
if [ -f "${PGDATA:-/var/lib/postgresql/data}/pg_hba.conf" ]; then
  PGDATA="${PGDATA:-/var/lib/postgresql/data}" \
    bash /docker-entrypoint-initdb.d/12_password_auth.sh || true
fi

# Hand off to the stock postgres entrypoint (initdb, auth, etc.); PostgreSQL
# listens on the internal port so only the proxy fronts it.
exec docker-entrypoint.sh postgres -p "$OBLAKO_PG_PORT"
