# Slim, self-contained Studio notebook image: JupyterLab + boto3, pre-wired so
# unmodified boto3 in the kernel hits oblako's local services. Built on first
# create_domain (tagged oblako/sagemaker-notebook:latest) — far leaner than
# jupyter/base-notebook (~1.4 GB) and ships boto3 + the AWS config baked in.
FROM python:3.12-slim

RUN pip install --no-cache-dir jupyterlab boto3 botocore && \
    rm -rf /root/.cache

# Pre-wire boto3 for oblako: path-style S3 (S3Proxy has no env knob for it) +
# flexible-checksums off + test creds. The per-service AWS_ENDPOINT_URL_* are
# injected at launch (they depend on host.docker.internal + the active ports).
COPY aws-config /etc/oblako/aws-config
ENV AWS_CONFIG_FILE=/etc/oblako/aws-config \
    AWS_ACCESS_KEY_ID=test \
    AWS_SECRET_ACCESS_KEY=test \
    AWS_DEFAULT_REGION=us-east-1 \
    AWS_REQUEST_CHECKSUM_CALCULATION=when_required \
    AWS_RESPONSE_CHECKSUM_VALIDATION=when_required

EXPOSE 8888
# The instance container idles; launch_notebook execs `jupyter lab` into it.
CMD ["sleep", "infinity"]
