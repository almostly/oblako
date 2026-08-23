# SageMaker "bring your own container" image for Automatic Model Tuning (HPO).
# Each tuning trial runs this image as one training job with a different `alpha`
# hyperparameter; the objective (validation MSE) is printed to stdout where
# oblako's tuner scrapes it with the job's MetricDefinitions regex. The training
# program is the image's ENTRYPOINT (see train_image for why).
FROM python:3.11-slim
COPY train.py /usr/local/bin/train
RUN chmod +x /usr/local/bin/train
ENTRYPOINT ["train"]
