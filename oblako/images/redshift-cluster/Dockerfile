# oblako redshift-local, MPP variant: the same Redshift-compatible engine, with
# Citus underneath so tables shard across worker nodes for real multi-node
# parallelism. It builds FROM the single-node image, so it keeps that image's
# whole compatibility layer (the oblako_redshift extension, the initdb SQL, the
# wire proxy, the COPY/UNLOAD bridge, the baked TLS cert), and compiles Citus from
# source for the build platform, so it is multi-arch (amd64 + arm64) and runs
# natively on Apple Silicon.

ARG BASE_IMAGE=public.ecr.aws/oblako/redshift-local:16

# --- build stage: compile Citus against the single-node image's PostgreSQL (built
# with 127-byte names, so extensions must be compiled against its headers)
FROM ${BASE_IMAGE} AS citus
ARG CITUS_VERSION=13.2.2
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc make autoconf ca-certificates curl \
        libcurl4-openssl-dev liblz4-dev libzstd-dev libssl-dev libkrb5-dev \
        libicu-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL "https://github.com/citusdata/citus/archive/refs/tags/v${CITUS_VERSION}.tar.gz" \
        | tar xz -C /tmp \
    && cd "/tmp/citus-${CITUS_VERSION}" \
    && ./configure --without-libcurl \
    && make -j"$(nproc)" \
    && make install DESTDIR=/out

# --- runtime: the single-node image plus Citus
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends \
        liblz4-1 libzstd1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=citus /out/ /

# Preload Citus first, then the oblako_redshift shim; the last setting in the
# sample config wins, so this replaces the single-node image's line.
RUN echo "shared_preload_libraries = 'citus,oblako_redshift'" \
        >> /usr/share/postgresql/postgresql.conf.sample

# CREATE EXTENSION citus runs before the single-node init scripts (00_...).
COPY 000_citus.sql 13_svv_table_info.sql /docker-entrypoint-initdb.d/

# On Citus the engine must NOT spoof server_version (it breaks CREATE EXTENSION
# citus), so the in-engine spoof is off and the wire proxy presents Redshift's
# version to clients instead. OBLAKO_CITUS=1 makes the proxy distribute DISTKEY /
# DISTSTYLE ALL tables (create_distributed_table / create_reference_table) after
# their CREATE commits.
ENV OBLAKO_SPOOF_SERVER_VERSION=off \
    OBLAKO_PROXY_SERVER_VERSION=8.0.2 \
    OBLAKO_CITUS=1

COPY cluster-entrypoint.sh /usr/local/bin/oblako-cluster-entrypoint.sh
RUN chmod +x /usr/local/bin/oblako-cluster-entrypoint.sh

EXPOSE 5439
ENTRYPOINT ["/usr/local/bin/oblako-cluster-entrypoint.sh"]
CMD ["postgres"]
