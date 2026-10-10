# oblako's Amazon Redshift: a modern PostgreSQL that impersonates Redshift, so
# redshift_connector (dbt-redshift, etc.) connects with no wire proxy. Built
# multi-arch (arm64 + amd64) so it runs on both Docker and Apple `container`.

# --- build stage: PostgreSQL with Redshift's 127-byte names, and the extension
# Redshift names (users, roles, schemas, tables, columns) run to 127 bytes;
# PostgreSQL's NAMEDATALEN is a compile-time constant (64: 63 bytes), so the
# engine is built from source, the same release as the base image, with
# NAMEDATALEN 128 and Debian's socket directory. It installs to /usr/local/pgsql;
# the stock build stays in /usr/lib/postgresql/16, which the entrypoint uses only
# to migrate a data directory made with 63-byte names (see migrate_names.sh).
FROM postgres:16 AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential bison flex pkg-config curl ca-certificates bzip2 \
        libreadline-dev zlib1g-dev libssl-dev libxml2-dev libxslt1-dev libicu-dev \
        liblz4-dev libzstd-dev uuid-dev python3-dev \
    && rm -rf /var/lib/apt/lists/*
RUN v="${PG_VERSION%%-*}" \
    && url="https://ftp.postgresql.org/pub/source/v${v}/postgresql-${v}.tar.bz2" \
    && curl -fsSL -o /tmp/postgresql.tar.bz2 "$url" \
    && echo "$(curl -fsSL "$url.sha256" | cut -d' ' -f1)  /tmp/postgresql.tar.bz2" \
        | sha256sum -c - \
    && tar xjf /tmp/postgresql.tar.bz2 -C /tmp \
    && cd "/tmp/postgresql-${v}" \
    && sed -i -e 's/^#define NAMEDATALEN 64$/#define NAMEDATALEN 128/' \
        -e 's|^#define DEFAULT_PGSOCKET_DIR .*|#define DEFAULT_PGSOCKET_DIR "/var/run/postgresql"|' \
        src/include/pg_config_manual.h \
    && grep -q '^#define NAMEDATALEN 128$' src/include/pg_config_manual.h \
    && ./configure --prefix=/usr/local/pgsql --with-python PYTHON=/usr/bin/python3 \
        --with-openssl --with-libxml --with-libxslt --with-icu --with-lz4 --with-zstd \
        --with-uuid=e2fs --with-system-tzdata=/usr/share/zoneinfo \
    && make -j"$(nproc)" world-bin \
    && make install-world-bin \
    && rm -rf /tmp/postgresql*
ENV PATH=/usr/local/pgsql/bin:$PATH
COPY ext/ /tmp/ext/
RUN make -C /tmp/ext && make -C /tmp/ext install   # -> /usr/local/pgsql/lib/oblako_redshift.so

# --- runtime: the 127-byte-name PostgreSQL, plpython3u, the built .so (no compilers)
FROM postgres:16

# python3 runs the Redshift-compat wire proxy and plpython3u (the Redshift UDFs and
# Redshift ML prediction UDFs); openssl generates the proxy's self-signed TLS cert
# at startup (see entrypoint). The stock postgresql-plpython3-16 is for the stock
# server that migrates an old data directory; libxslt is the new build's.
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-plpython3-16 python3 python3-pip openssl libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/pgsql /usr/local/pgsql
ENV PATH=/usr/local/pgsql/bin:$PATH
# initdb takes its config from the base image's sample (listen_addresses = '*',
# which the cluster's nodes need), and the settings below are appended to it
RUN ln -sf /usr/share/postgresql/postgresql.conf.sample \
        /usr/local/pgsql/share/postgresql.conf.sample

# boto3 + pyarrow + pydantic power the COPY/UNLOAD <-> S3 bridge: the proxy
# rewrites Redshift COPY/UNLOAD into calls to plpython3u functions that read/write
# Parquet on S3 (see initdb.d/07_copy_unload.sql, proxy/copy_unload.py). Installed
# into the SYSTEM python so plpython3u (which links the system libpython) can
# import them; --break-system-packages is required on Debian's PEP 668 python.
RUN pip3 install --no-cache-dir --break-system-packages \
        boto3 pyarrow pydantic sqlglot \
    && rm -rf /root/.cache
# pyiceberg writes Redshift's Iceberg tables (CREATE TABLE ... USING ICEBERG) to
# oblako's Iceberg REST catalog, through s3fs to S3Proxy (see initdb.d/13_iceberg.sql).
RUN pip3 install --no-cache-dir --break-system-packages "pyiceberg[s3fs,pyiceberg-core]" \
    && rm -rf /root/.cache
# docker + psycopg run the Redshift ML training agent (proxy/redshift_ml.py): it
# reads queued CREATE MODELs over the local socket and trains them in a container
# on the host Docker daemon (via a mounted /var/run/docker.sock).
RUN pip3 install --no-cache-dir --break-system-packages \
        docker "psycopg[binary]" \
    && rm -rf /root/.cache

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
# Iceberg tables: DDL rewriting (proxy) and the plpython3u catalog work.
COPY proxy/iceberg_tables.py /usr/local/bin/iceberg_tables.py
# Dynamic data masking: policy DDL rewriting (proxy), the catalog in
# initdb.d/15_masking.sql.
COPY proxy/masking.py /usr/local/bin/masking.py
# Quoted object names folded to lower case, as Redshift does; imported by the proxy.
COPY proxy/identifiers.py /usr/local/bin/identifiers.py
# GRANT/REVOKE of ALTER and DROP -> pg_oblako calls; imported by the proxy.
COPY proxy/object_privileges.py /usr/local/bin/object_privileges.py
COPY entrypoint.sh /usr/local/bin/oblako-entrypoint.sh
COPY migrate_names.sh /usr/local/bin/oblako-migrate-names.sh
RUN chmod +x /usr/local/bin/oblako-entrypoint.sh /usr/local/bin/oblako-migrate-names.sh

EXPOSE 5439
ENTRYPOINT ["/usr/local/bin/oblako-entrypoint.sh"]
CMD ["postgres"]
