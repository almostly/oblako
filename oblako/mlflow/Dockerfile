FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# MLflow 3+ tracking server, with sagemaker-mlflow for SigV4-signed clients
# targeting SageMaker-managed MLflow, plus psycopg2 in case the backend is
# pointed at oblako's RDS Postgres.
RUN pip install --no-cache-dir \
    "mlflow>=3" \
    "sagemaker-mlflow>=0.1" \
    "boto3" \
    "psycopg2-binary"

# Bake path-style + checksum config so MLflow's boto3 talks to S3Proxy correctly
# without needing to mount an AWS config file at runtime.
RUN mkdir -p /root/.aws \
 && printf '[default]\nregion = us-east-1\nrequest_checksum_calculation = when_required\nresponse_checksum_validation = when_required\ns3 =\n    addressing_style = path\n' > /root/.aws/config

VOLUME ["/data"]
EXPOSE 5050

# Proxied artifact storage (default-artifact-root falls back to mlflow-artifacts:/)
# so clients upload via the server's HTTP API and only the server hits S3.
ENTRYPOINT ["mlflow"]
CMD ["server", "--host", "0.0.0.0", "--port", "5050", \
     "--backend-store-uri", "sqlite:////data/mlflow.db", \
     "--artifacts-destination", "s3://oblako-mlflow"]
