# Redshift ML training image — real scikit-learn + xgboost, runs as a SageMaker
# local job. Inference happens later in pgredshift as a pure-Python UDF.
FROM python:3.11-slim
RUN pip install --no-cache-dir scikit-learn xgboost
COPY train.py /usr/local/bin/train
RUN chmod +x /usr/local/bin/train
# SageMaker SDK v3 (ModelTrainer, local mode) runs the image's own ENTRYPOINT
# rather than passing the legacy `train` command, so make training the entrypoint.
# It ignores argv, so a stray `train` arg (the v2 convention) is harmless.
ENTRYPOINT ["train"]
