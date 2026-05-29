# Bring-your-own-container that both TRAINS and SERVES a CatBoost credit scorecard.
# SageMaker runs it as `<image> train` (training job) and `<image> serve` (endpoint).
FROM python:3.11-slim

# libgomp1: CatBoost needs OpenMP at runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir catboost numpy pandas joblib

COPY src/ /opt/program/
WORKDIR /opt/program
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "main.py"]
