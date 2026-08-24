# Minimal SageMaker "bring your own container" processing image — reads
# /opt/ml/processing/input, writes /opt/ml/processing/output, fully local.
# The processing program is the image's ENTRYPOINT (how v3 and oblako invoke it).
FROM python:3.11-slim
COPY process.py /usr/local/bin/process
RUN chmod +x /usr/local/bin/process
ENTRYPOINT ["process"]
