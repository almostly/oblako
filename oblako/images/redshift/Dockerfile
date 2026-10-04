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
        postgresql-plpython3-16 python3 python3-pip openssl \
    && rm -rf /var/lib/apt/lists/*

# boto3 + pyarrow + pydantic power the COPY/UNLOAD <-> S3 bridge: the proxy
# rewrites Redshift COPY/UNLOAD into calls to plpython3u functions that read/write
# Parquet on S3 (see initdb.d/07_copy_unload.sql, proxy/copy_unload.py). Installed
# into the SYSTEM python so plpython3u (which links the system libpython) can
# import them; --break-system-packages is required on Debian's PEP 668 python.
RUN pip3 install --no-cache-dir --break-system-packages \
        boto3 pyarrow pydantic sqlglot \
    && rm -rf /root/.cache
# docker + psycopg run the Redshift ML training agent (proxy/redshift_ml.py): it
# reads queued CREATE MODELs over the local socket and trains them in a container
# on the host Docker daemon (via a mounted /var/run/docker.sock).
RUN pip3 install --no-cache-dir --break-system-packages \
        docker "psycopg[binary]" \
    && rm -rf /root/.cache

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
# The COPY/UNLOAD <-> S3 bridge, shared by the proxy (rewrites COPY/UNLOAD into
# oblako_* calls) and the plpython3u functions (do the S3 work). On the proxy's
# script dir and, via PYTHONPATH in the entrypoint, on plpython's import path.
COPY proxy/copy_unload.py /usr/local/bin/copy_unload.py
# SUPER (PartiQL) dot-navigation rewriting, imported by the proxy.
COPY proxy/super_nav.py /usr/local/bin/super_nav.py
# LISTAGG -> string_agg rewriting, imported by the proxy.
COPY proxy/listagg.py /usr/local/bin/listagg.py
# Bare datepart keywords (DATEADD(month, ...)) -> quoted, imported by the proxy.
COPY proxy/datepart.py /usr/local/bin/datepart.py
# AVG(...) -> pg_oblako.avg(...) (Redshift's BIGINT result for integers).
COPY proxy/integer_avg.py /usr/local/bin/integer_avg.py
# Redshift ML: CREATE/SHOW/DROP MODEL rewriting (proxy), the plpython3u entry
# points (initdb.d/09_redshift_ml.sql) and the training agent, plus the training
# image's build context the agent builds on first use.
COPY proxy/redshift_ml.py /usr/local/bin/redshift_ml.py
COPY ml_train/ /usr/local/share/oblako/ml_train/
# PIVOT/UNPIVOT -> standard SQL (sqlglot-parsed), imported by the proxy.
COPY proxy/pivot_unpivot.py /usr/local/bin/pivot_unpivot.py
COPY entrypoint.sh /usr/local/bin/oblako-entrypoint.sh
RUN chmod +x /usr/local/bin/oblako-entrypoint.sh

EXPOSE 5439
ENTRYPOINT ["/usr/local/bin/oblako-entrypoint.sh"]
CMD ["postgres"]
