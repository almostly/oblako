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
# a cert for this container, its subject made unique by a random OU (OpenSSL finds
# a trusted self-signed cert by subject, so two trusted oblako certs must not
# share one). Disable TLS entirely with OBLAKO_SSL=0.
CERT_DIR=/etc/oblako-redshift
export OBLAKO_SSL_CERT="${OBLAKO_SSL_CERT:-$CERT_DIR/server.crt}"
export OBLAKO_SSL_KEY="${OBLAKO_SSL_KEY:-$CERT_DIR/server.key}"
if [ "${OBLAKO_SSL:-1}" = "1" ] && [ ! -f "$OBLAKO_SSL_CERT" ]; then
  mkdir -p "$(dirname "$OBLAKO_SSL_CERT")"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$OBLAKO_SSL_KEY" -out "$OBLAKO_SSL_CERT" \
    -subj "/O=oblako/OU=$(openssl rand -hex 6)/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
    -addext "basicConstraints=critical,CA:FALSE" -addext "extendedKeyUsage=serverAuth" \
    >/dev/null 2>&1 || echo "oblako: could not generate TLS cert; proxy will run without SSL"
fi

# Start the proxy once PostgreSQL is accepting connections on the internal port.
# First bring every database up to date: Redshift's system objects in pg_catalog
# (initdb.d/99_system_catalog.sql), the pg_oblako AVG aggregates the
# proxy routes avg() to (initdb.d/11_integer_avg.sql), and the Iceberg table
# functions (initdb.d/13_iceberg.sql), and Redshift's identity and privilege
# views (initdb.d/14_redshift_identities.sql), and the masking policy catalog
# (initdb.d/15_masking.sql). initdb scripts run only on a fresh volume, and these
# must also reach databases created before they existed. All five scripts are
# idempotent.
(
  until pg_isready -h 127.0.0.1 -p "$OBLAKO_PG_PORT" -q 2>/dev/null; do
    sleep 0.5
  done
  # a data directory moved from 63-byte names gets its dumps back first
  /usr/local/bin/oblako-migrate-names.sh restore ||
    echo "oblako: could not restore the data directory's dumps"
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
    psql -U "${POSTGRES_USER:-postgres}" -p "$OBLAKO_PG_PORT" -d "$db" -q \
      -f /docker-entrypoint-initdb.d/13_iceberg.sql >/dev/null 2>&1 \
      || echo "oblako: could not install the Iceberg table functions in $db"
    psql -U "${POSTGRES_USER:-postgres}" -p "$OBLAKO_PG_PORT" -d "$db" -q \
      -f /docker-entrypoint-initdb.d/14_redshift_identities.sql >/dev/null 2>&1 \
      || echo "oblako: could not install the Redshift identity views in $db"
    psql -U "${POSTGRES_USER:-postgres}" -p "$OBLAKO_PG_PORT" -d "$db" -q \
      -f /docker-entrypoint-initdb.d/15_masking.sql >/dev/null 2>&1 \
      || echo "oblako: could not install the masking policy catalog in $db"
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

# A data directory made with PostgreSQL's 63-byte names is dumped and moved aside
# here, so the stock entrypoint initializes one with Redshift's 127-byte names;
# the background step above restores it (see migrate_names.sh).
/usr/local/bin/oblako-migrate-names.sh dump

# A data directory initialized before loopback TCP followed POSTGRES_HOST_AUTH_METHOD
# (initdb.d/12_password_auth.sh) gets the same change here, on every start.
if [ -f "${PGDATA:-/var/lib/postgresql/data}/pg_hba.conf" ]; then
  PGDATA="${PGDATA:-/var/lib/postgresql/data}" \
    bash /docker-entrypoint-initdb.d/12_password_auth.sh || true
fi

# Hand off to the stock postgres entrypoint (initdb, auth, etc.); PostgreSQL
# listens on the internal port so only the proxy fronts it.
exec docker-entrypoint.sh postgres -p "$OBLAKO_PG_PORT"
