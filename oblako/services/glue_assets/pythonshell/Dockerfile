# Glue Python shell 3.9: a plain Python process with Glue's library set, no Spark.
FROM python:3.9-slim
ARG LIBRARY_SET=analytics
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# psycopg2, pyodbc and pyorc build from source where no wheel fits
COPY requirements-${LIBRARY_SET}.txt /tmp/requirements.txt
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake libpq-dev unixodbc-dev \
 && pip install -r /tmp/requirements.txt \
 && apt-get purge -y build-essential cmake \
 && apt-get autoremove -y \
 && apt-get install -y --no-install-recommends libpq5 unixodbc \
 && rm -rf /var/lib/apt/lists/* /tmp/requirements.txt

# awsglue.utils (getResolvedOptions) and the endpoint hook for the old botocore
COPY site /opt/oblako
ENV PYTHONPATH=/opt/oblako

# S3Proxy needs path-style addressing
RUN mkdir -p /root/.aws \
 && printf '[default]\ns3 =\n    addressing_style = path\n' > /root/.aws/config
