# Bring-your-own-container that TRAINS a linear fraud classifier (hinge-loss SVM).
# SageMaker runs it as `<image> train` (a local-mode training job).
FROM python:3.11-slim

RUN pip install --no-cache-dir scikit-learn pandas joblib

COPY src/ /opt/program/
WORKDIR /opt/program
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "main.py"]
