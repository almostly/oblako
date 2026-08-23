# Minimal SageMaker "bring your own container" inference image — serves the model
# over HTTP on :8080 (GET /ping, POST /invocations), fully local, no ECR pull.
# The server is the image's ENTRYPOINT, which is how the v3 SDK and oblako's
# container runner invoke a serving image (v2 passed a `serve` command; the
# entrypoint ignores argv, so that still works).
FROM python:3.11-slim
COPY serve.py /usr/local/bin/serve
RUN chmod +x /usr/local/bin/serve
EXPOSE 8080
ENTRYPOINT ["serve"]
