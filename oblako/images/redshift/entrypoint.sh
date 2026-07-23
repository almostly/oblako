#!/usr/bin/env bash
# Run PostgreSQL on an internal port and the Redshift-compat wire proxy on the
# published port, so clients transparently get Redshift-tolerant SQL (the proxy
# strips DISTSTYLE/DISTKEY/SORTKEY/ENCODE that PostgreSQL can't parse). From the
# outside it's still a single container listening on the usual port.
set -e

export OBLAKO_PG_PORT="${OBLAKO_PG_PORT:-5433}"       # PostgreSQL, internal only
export OBLAKO_PROXY_PORT="${OBLAKO_PROXY_PORT:-5439}" # what clients connect to (Redshift's port)
export OBLAKO_PG_HOST=127.0.0.1

# TLS cert for the proxy. The image bakes a FIXED self-signed cert at
# /etc/oblako-redshift (so every container presents the same cert, stable across
# `down -v`/clones, keeping pins + `oblako trust` valid). Override by mounting
# your own there. This block only generates one as a fallback if none is present
# (e.g. an empty mounted override). Disable TLS entirely with OBLAKO_SSL=0.
CERT_DIR=/etc/oblako-redshift
export OBLAKO_SSL_CERT="${OBLAKO_SSL_CERT:-$CERT_DIR/server.crt}"
export OBLAKO_SSL_KEY="${OBLAKO_SSL_KEY:-$CERT_DIR/server.key}"
if [ "${OBLAKO_SSL:-1}" = "1" ] && [ ! -f "$OBLAKO_SSL_CERT" ]; then
  mkdir -p "$(dirname "$OBLAKO_SSL_CERT")"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$OBLAKO_SSL_KEY" -out "$OBLAKO_SSL_CERT" \
    -subj "/O=oblako/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
    >/dev/null 2>&1 || echo "oblako: could not generate TLS cert; proxy will run without SSL"
fi

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
