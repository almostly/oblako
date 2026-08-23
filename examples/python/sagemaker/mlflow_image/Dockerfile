# SageMaker "bring your own container" training image demonstrating the
# serverless MLflow pattern: the run is tracked to a sqlite database on
# /opt/ml/model (the writable mount SageMaker collects into model.tar.gz), so the
# experiment travels WITH the model and needs no running MLflow server. The one
# env that switches worlds is MLFLOW_TRACKING_URI: a sqlite path locally, a
# SageMaker MLflow App ARN on AWS. The training program is the image ENTRYPOINT.
FROM python:3.11-slim
RUN pip install --no-cache-dir "mlflow>=2.9,<3"
COPY train.py /usr/local/bin/train
RUN chmod +x /usr/local/bin/train
ENTRYPOINT ["train"]
