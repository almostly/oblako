"""Local AWS Glue: PySpark jobs in the official ``amazon/aws-glue-libs:5`` image.

Submit a Glue / PySpark script and the service runs a per-job container that has
Spark + Glue libs + Iceberg already on the classpath. The container is wired so
Spark can reach oblako's Iceberg REST catalog and S3Proxy on the host
(``host.docker.internal``), so a Glue job can read/write the same Iceberg tables
pyiceberg sees.

The Glue 5 image is ~5 GB — pulled lazily by :meth:`ensure_image` on first use.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from oblako import config

IMAGE_TAG = "amazon/aws-glue-libs:5"


class GlueService:
    """Per-job Glue 5 runner. Submit a PySpark script, get back logs + exit code."""

    name = "glue"

    def __init__(self):
        """Initialize a Glue runner (no persistent container — jobs run on demand)."""
        self._client = None

    @property
    def client(self):
        """Return the docker-py client, honouring DOCKER_HOST or the active context."""
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    def ensure_image(self) -> None:
        """Pull the Glue 5 image (~5 GB) if it isn't present locally."""
        from docker.errors import ImageNotFound

        try:
            self.client.images.get(IMAGE_TAG)
        except ImageNotFound:
            print(f"Pulling {IMAGE_TAG} (~5 GB) — this can take a few minutes...")
            self.client.images.pull(IMAGE_TAG)

    def submit_job(self, script: str, *, args: list[str] | None = None,
                   env: dict[str, str] | None = None, timeout: int = 1200) -> dict:
        """Run a PySpark script in a Glue 5 container; return ``{exit_code, logs}``.

        The script is written to a tempdir mounted at ``/scripts/job.py``. The
        container is given creds + endpoints to reach oblako's S3Proxy and Iceberg
        REST catalog at ``host.docker.internal``.
        """
        self.ensure_image()
        full_env = {
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": config.region(),
            # S3Proxy doesn't speak the new flexible checksums.
            "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
            "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
            **(env or {}),
        }
        with tempfile.TemporaryDirectory() as scripts_dir:
            (Path(scripts_dir) / "job.py").write_text(script)
            container = self.client.containers.run(
                IMAGE_TAG,
                command=["spark-submit", "/scripts/job.py", *(args or [])],
                detach=True,
                volumes={scripts_dir: {"bind": "/scripts", "mode": "ro"}},
                environment=full_env,
                extra_hosts={"host.docker.internal": "host-gateway"},
            )
            try:
                result = container.wait(timeout=timeout)
                logs = container.logs().decode("utf-8", errors="replace")
                return {"exit_code": result["StatusCode"], "logs": logs}
            finally:
                container.remove(force=True)
