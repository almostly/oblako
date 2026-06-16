# oblako's Amazon Redshift: a modern PostgreSQL that impersonates Redshift, so
# redshift_connector (dbt-redshift, etc.) connects with no wire proxy. Built
# multi-arch (arm64 + amd64) so it runs on both Docker and Apple `container`.
FROM postgres:16

# plpython3u for the Redshift UDFs / Redshift ML prediction UDFs, and the build
# toolchain for the compatibility extension.
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-plpython3-16 \
        postgresql-server-dev-16 gcc make \
    && rm -rf /var/lib/apt/lists/*

# Build + install the shared_preload compatibility extension.
COPY ext/ /tmp/ext/
RUN make -C /tmp/ext && make -C /tmp/ext install && rm -rf /tmp/ext

# Load it at startup (registers the Redshift startup GUCs + server_version 8.0.2).
RUN echo "shared_preload_libraries = 'oblako_redshift'" \
    >> /usr/share/postgresql/postgresql.conf.sample

# Redshift (and pgredshift/PG10) use md5 password auth; redshift_connector's
# handshake expects it. PG16 defaults to scram-sha-256, so pin md5 — stored
# passwords must be md5 too, so run with POSTGRES_HOST_AUTH_METHOD=md5.
RUN echo "password_encryption = md5" \
    >> /usr/share/postgresql/postgresql.conf.sample

# System tables + UDFs run on first DB init.
COPY initdb.d/ /docker-entrypoint-initdb.d/
