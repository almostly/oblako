# oblako's Amazon Redshift: a modern PostgreSQL that impersonates Redshift, so
# redshift_connector (dbt-redshift, etc.) connects with no wire proxy. Built
# multi-arch (arm64 + amd64) so it runs on both Docker and Apple `container`.

# --- build stage: compile the compatibility extension, then throw the toolchain away
FROM postgres:16 AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-server-dev-16 gcc make \
    && rm -rf /var/lib/apt/lists/*
COPY ext/ /tmp/ext/
RUN make -C /tmp/ext && make -C /tmp/ext install   # -> $pkglibdir/oblako_redshift.so

# --- runtime: stock postgres + plpython3u + the built .so (no compilers/headers)
FROM postgres:16

# plpython3u for the Redshift UDFs / Redshift ML prediction UDFs (runtime only).
# python3 also runs the Redshift-compat wire proxy; openssl generates the proxy's
# self-signed TLS cert at startup (see entrypoint).
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-plpython3-16 python3 openssl \
    && rm -rf /var/lib/apt/lists/*

# The shared_preload extension, compiled in the build stage.
COPY --from=build /usr/lib/postgresql/16/lib/oblako_redshift.so /usr/lib/postgresql/16/lib/

# Load it at startup (registers the Redshift startup GUCs + server_version 8.0.2).
RUN echo "shared_preload_libraries = 'oblako_redshift'" \
    >> /usr/share/postgresql/postgresql.conf.sample

# Redshift (and pgredshift/PG10) use md5 password auth; redshift_connector's
# handshake expects it. PG16 defaults to scram-sha-256, so pin md5; stored
# passwords must be md5 too, so run with POSTGRES_HOST_AUTH_METHOD=md5.
RUN echo "password_encryption = md5" \
    >> /usr/share/postgresql/postgresql.conf.sample

# System tables + UDFs run on first DB init.
COPY initdb.d/ /docker-entrypoint-initdb.d/

# Redshift-compat wire proxy: PostgreSQL can't parse DISTSTYLE/DISTKEY/SORTKEY/
# ENCODE (rejected at parse time, before any extension runs), so a thin proxy in
# front of the engine strips them. It listens on 5439 (Redshift's real port, so
# it doesn't collide with a plain Postgres on 5432 in the same network);
# PostgreSQL moves to an internal port. Transparent for everything else (auth,
# COPY, ...). Override the listen port with OBLAKO_PROXY_PORT.
COPY proxy/redshift_proxy.py /usr/local/bin/redshift_proxy.py
COPY entrypoint.sh /usr/local/bin/oblako-entrypoint.sh
RUN chmod +x /usr/local/bin/oblako-entrypoint.sh

# Fixed self-signed TLS cert (local dev only), baked so every container presents
# the SAME cert, stable across `down -v` and fresh clones, which keeps a pinned
# sslrootcert or `oblako trust` valid. Not a secret (self-signed, localhost only);
# override by mounting your own at /etc/oblako-redshift. See certs/README.md.
COPY certs/server.crt certs/server.key /etc/oblako-redshift/
RUN chmod 600 /etc/oblako-redshift/server.key

EXPOSE 5439
ENTRYPOINT ["/usr/local/bin/oblako-entrypoint.sh"]
CMD ["postgres"]
