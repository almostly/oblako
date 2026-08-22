# oblako redshift-local, MPP variant: the same Redshift-compatible engine, but on
# Citus so tables shard across worker nodes for real multi-node parallelism. It
# reuses the single-node image's whole compat layer (the oblako_redshift C
# extension, the initdb SQL, the wire proxy, the baked TLS cert) and layers Citus
# underneath, preloaded alongside oblako_redshift.
#
# amd64 only: Citus publishes no arm64 image, so on Apple Silicon this runs under
# emulation (fine for correctness, not for speed). See oblako/images/redshift for
# the single-node image this builds on.
FROM citusdata/citus:12.1

# plpython3u for the Redshift UDFs / Redshift ML prediction UDFs; python3 runs the
# wire proxy; openssl is only a fallback (the TLS cert is baked, copied below).
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-plpython3-16 python3 openssl \
    && rm -rf /var/lib/apt/lists/*

# Reuse the single-node image's artifacts instead of rebuilding them: the compat
# C extension, the initdb SQL (system tables, UDFs, date functions, catalog
# views), the wire proxy + its entrypoint, and the baked TLS cert. The extension
# is built for PostgreSQL 16, ABI-compatible with Citus's PG 16.
ARG BASE_IMAGE=public.ecr.aws/oblako/redshift-local:16
COPY --from=public.ecr.aws/oblako/redshift-local:16 \
     /usr/lib/postgresql/16/lib/oblako_redshift.so /usr/lib/postgresql/16/lib/
COPY --from=public.ecr.aws/oblako/redshift-local:16 \
     /docker-entrypoint-initdb.d/ /docker-entrypoint-initdb.d/
COPY --from=public.ecr.aws/oblako/redshift-local:16 \
     /usr/local/bin/redshift_proxy.py /usr/local/bin/oblako-entrypoint.sh /usr/local/bin/
COPY --from=public.ecr.aws/oblako/redshift-local:16 \
     /etc/oblako-redshift/ /etc/oblako-redshift/
RUN chmod 600 /etc/oblako-redshift/server.key \
    && chmod +x /usr/local/bin/oblako-entrypoint.sh

# Preload Citus first (it detects the real PG 16), then the oblako_redshift shim
# (no-op Redshift GUCs + server_version spoof, applied afterwards). md5 auth to
# match redshift_connector, exactly as the single-node image does.
RUN echo "shared_preload_libraries = 'citus,oblako_redshift'" \
        >> /usr/share/postgresql/postgresql.conf.sample \
    && echo "password_encryption = md5" \
        >> /usr/share/postgresql/postgresql.conf.sample

# On Citus the engine must NOT spoof server_version (it breaks CREATE EXTENSION
# citus), so disable the in-engine spoof and have the wire proxy present Redshift's
# version to clients instead. The engine keeps its real version for Citus.
# OBLAKO_CITUS=1 also makes the proxy auto-distribute DISTKEY / DISTSTYLE ALL
# tables (create_distributed_table / create_reference_table) after CREATE commits.
ENV OBLAKO_SPOOF_SERVER_VERSION=off \
    OBLAKO_PROXY_SERVER_VERSION=8.0.2 \
    OBLAKO_CITUS=1

COPY cluster-entrypoint.sh /usr/local/bin/oblako-cluster-entrypoint.sh
RUN chmod +x /usr/local/bin/oblako-cluster-entrypoint.sh

EXPOSE 5439
ENTRYPOINT ["/usr/local/bin/oblako-cluster-entrypoint.sh"]
CMD ["postgres"]
