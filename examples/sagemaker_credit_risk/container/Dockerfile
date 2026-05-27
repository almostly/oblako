# Bring-your-own-container that both TRAINS and SERVES a credit-scoring model.
# SageMaker runs it as `<image> train` (training job) and `<image> serve` (endpoint).
FROM python:3.11-slim

RUN pip install --no-cache-dir scikit-learn pandas numpy joblib

COPY src/ /opt/program/
WORKDIR /opt/program
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "main.py"]
