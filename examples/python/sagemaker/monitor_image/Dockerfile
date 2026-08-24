# SageMaker Model Monitor "bring your own container" analyzer. Model Monitor runs
# as a processing job that reads an endpoint's captured invocations (Data Capture
# JSONL) from /opt/ml/processing/input/endpoint and writes a statistics +
# constraint-violations report to /opt/ml/processing/output. The analyzer is the
# image ENTRYPOINT (as with the other BYOC images).
FROM python:3.11-slim
COPY process.py /usr/local/bin/process
RUN chmod +x /usr/local/bin/process
ENTRYPOINT ["process"]
