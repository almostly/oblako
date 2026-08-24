# Minimal SageMaker "bring your own container" training image — fully local,
# no AWS Deep Learning Container / ECR pull. The training program is the image's
# ENTRYPOINT, which is how the v3 SDK (and oblako's own container runner) invoke
# a BYOC training image (v2 passed a `train` command; the entrypoint ignores argv,
# so that still works).
FROM python:3.11-slim
COPY train.py /usr/local/bin/train
RUN chmod +x /usr/local/bin/train
ENTRYPOINT ["train"]
