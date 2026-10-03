#!/usr/bin/env bash
# Loopback TCP uses the same authentication as every other host. The wire proxy
# reaches PostgreSQL over 127.0.0.1 and relays each client's login, and initdb
# trusts loopback by default, which would accept any password even when
# POSTGRES_HOST_AUTH_METHOD asks for md5. With md5 (oblako's default) passwords
# are checked, as on Redshift; a deployment that sets trust keeps passwordless
# logins. The Unix socket stays trusted for in-container tools.
set -e
method="${POSTGRES_HOST_AUTH_METHOD:-md5}"
sed -i -E "s/^(host[[:space:]]+all[[:space:]]+all[[:space:]]+(127\.0\.0\.1\/32|::1\/128)[[:space:]]+)[a-z0-9-]+\$/\1${method}/" \
    "$PGDATA/pg_hba.conf"
