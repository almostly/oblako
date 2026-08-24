# SageMaker multi-model endpoint (MME) serving image. One container serves many
# models: SageMaker sends the model to use in the X-Amzn-SageMaker-Target-Model
# header, and oblako has placed that model under /opt/ml/models/<TargetModel>/
# before the call. The server loads the target model on first use and caches it.
# The server is the image ENTRYPOINT (as with the other BYOC images).
FROM python:3.11-slim
COPY serve.py /usr/local/bin/serve
RUN chmod +x /usr/local/bin/serve
ENTRYPOINT ["serve"]
