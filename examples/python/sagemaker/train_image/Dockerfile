# Minimal SageMaker "bring your own container" training image — fully local,
# no AWS Deep Learning Container / ECR pull. SageMaker runs `docker run <img> train`.
FROM python:3.11-slim
COPY train.py /usr/local/bin/train
RUN chmod +x /usr/local/bin/train
