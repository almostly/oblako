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

# Glue's bundled AWS SDK adds `x-amz-optional-object-attributes: RestoreStatus`
# to listObjectsV2 (a Glacier restore-status optimization). S3Proxy 501s on it
# ("functionality not implemented"). A tiny SDK ExecutionInterceptor strips the
# header before signing; it's registered on every job via this S3A config.
_INTERCEPTOR_CLASS = "io.oblako.glue.StripOptionalObjectAttributes"
_INTERCEPTOR_CONF = (
    f"spark.hadoop.fs.s3a.audit.execution.interceptors={_INTERCEPTOR_CLASS}"
)


class GlueService:
    """Per-job Glue 5 runner. Submit a PySpark script, get back logs + exit code."""

    name = "glue"

    def __init__(self):
        """Initialize a Glue runner (no persistent container — jobs run on demand)."""
        self._client = None

    @property
    def client(self):
        """Return the docker-py client for the configured backend (docker/podman/colima)."""
        if self._client is None:
            from .backends import docker_client

            self._client = docker_client()
        return self._client

    def ensure_image(self) -> None:
        """Pull the Glue 5 image (~5 GB) if it isn't present locally."""
        from docker.errors import ImageNotFound

        try:
            self.client.images.get(IMAGE_TAG)
        except ImageNotFound:
            print(f"Pulling {IMAGE_TAG} (~5 GB) — this can take a few minutes...")
            self.client.images.pull(IMAGE_TAG)

    def _ensure_interceptor_jar(self) -> Path:
        """Build (once, cached) the S3A header-stripping interceptor jar.

        Compiled in the Glue image itself (it ships javac 17 + the AWS SDK), so
        no host Java toolchain is needed. Returns the host path to the jar, which
        ``submit_job`` mounts onto the Spark classpath.
        """
        jar = Path(tempfile.gettempdir()) / "oblako-glue" / "oblako-glue-s3a.jar"
        if jar.exists():
            return jar
        jar.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_image()
        src_dir = Path(__file__).parent / "glue_assets"
        build = (
            'set -e; BUNDLE=$(find /root/.m2 -name "bundle-*.jar" | head -1); '
            "mkdir -p /tmp/c && "
            'javac -cp "$BUNDLE" -d /tmp/c /src/StripOptionalObjectAttributes.java && '
            "(cd /tmp/c && jar cf /out/oblako-glue-s3a.jar io)"
        )
        container = self.client.containers.run(
            IMAGE_TAG,
            entrypoint=["bash", "-lc"],
            command=[build],
            detach=True,
            volumes={
                str(src_dir): {"bind": "/src", "mode": "ro"},
                str(jar.parent): {"bind": "/out", "mode": "rw"},
            },
        )
        try:
            result = container.wait(timeout=180)
            if result["StatusCode"] != 0 or not jar.exists():
                logs = container.logs().decode("utf-8", errors="replace")
                raise RuntimeError(f"interceptor jar build failed:\n{logs[-2000:]}")
        finally:
            container.remove(force=True)
        return jar

    def submit_job(
        self,
        script: str,
        *,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 1200,
    ) -> dict:
        """Run a PySpark script in a Glue 5 container; return ``{exit_code, logs}``.

        The script is written to a tempdir mounted at ``/scripts/job.py``. The
        container is given creds + endpoints to reach oblako's S3Proxy and Iceberg
        REST catalog at ``host.docker.internal``, and an S3A interceptor that
        strips the one header S3Proxy can't handle (see ``_INTERCEPTOR_CLASS``).
        """
        self.ensure_image()
        interceptor_jar = self._ensure_interceptor_jar()
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
                command=[
                    "spark-submit",
                    "--conf",
                    _INTERCEPTOR_CONF,
                    "/scripts/job.py",
                    *(args or []),
                ],
                detach=True,
                volumes={
                    scripts_dir: {"bind": "/scripts", "mode": "ro"},
                    # drop the interceptor into a classpath-glob dir so S3A loads it
                    str(interceptor_jar): {
                        "bind": "/opt/spark/jars/oblako-glue-s3a.jar",
                        "mode": "ro",
                    },
                },
                environment=full_env,
                extra_hosts={"host.docker.internal": "host-gateway"},
            )
            try:
                result = container.wait(timeout=timeout)
                logs = container.logs().decode("utf-8", errors="replace")
                return {"exit_code": result["StatusCode"], "logs": logs}
            finally:
                container.remove(force=True)

    def run_workflow(self, name: str, steps: list[dict], *, timeout: int = 600) -> dict:
        """Run a Glue workflow: a sequential pipeline of PySpark job steps.

        Each step is ``{name, script}``. Steps run in order, and a step runs only
        if every prior step SUCCEEDED — mirroring a Glue CONDITIONAL trigger gated
        on predecessor success. The first failure stops the pipeline; the rest are
        marked SKIPPED. Returns ``{name, status, steps:[{name, status, exitCode,
        logs}]}``.

        This covers the common sequential-ETL workflow. Full DAGs (parallel
        branches, crawlers, scheduled/event triggers) aren't modelled yet.
        """
        results: list[dict] = []
        failed = False
        for i, step in enumerate(steps):
            sname = step.get("name") or f"step{i + 1}"
            script = (step.get("script") or "").strip()
            if failed:
                results.append(
                    {"name": sname, "status": "SKIPPED", "exitCode": None, "logs": ""}
                )
                continue
            if not script:
                results.append(
                    {
                        "name": sname,
                        "status": "FAILED",
                        "exitCode": None,
                        "logs": "empty script",
                    }
                )
                failed = True
                continue
            try:
                r = self.submit_job(script, timeout=timeout)
            except Exception as e:  # noqa: BLE001 — surface the failure in the step
                results.append(
                    {
                        "name": sname,
                        "status": "FAILED",
                        "exitCode": None,
                        "logs": str(e),
                    }
                )
                failed = True
                continue
            ok = r["exit_code"] == 0
            results.append(
                {
                    "name": sname,
                    "status": "SUCCEEDED" if ok else "FAILED",
                    "exitCode": r["exit_code"],
                    "logs": r["logs"][-8000:],
                }
            )
            if not ok:
                failed = True
        return {
            "name": name,
            "status": "FAILED" if failed else "SUCCEEDED",
            "steps": results,
        }
