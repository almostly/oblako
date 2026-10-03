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

from oblako import config, ports

IMAGE_TAG = "amazon/aws-glue-libs:5"

# Glue's bundled AWS SDK v2 trips over two gaps in oblako's S3Proxy backend: it
# 501s on the `x-amz-optional-object-attributes` header the SDK adds to
# listObjectsV2, and it omits `<Size>` for the zero-byte directory markers Spark
# writes (so hadoop-aws NPEs on S3Object.size() at commit). A tiny SDK
# ExecutionInterceptor fixes both (strip the request header; default null list
# sizes to 0); registered on every job via this S3A config.
_INTERCEPTOR_CLASS = "io.oblako.glue.S3ProxyCompatInterceptor"
# Spark/Hadoop config every job needs for S3A to work against the S3Proxy backend.
_SPARK_CONFS = (
    # the compat interceptor: strips the 501 header, fills null marker fields,
    # and turns S3Proxy's 500 on a single directory-marker DELETE into success
    f"spark.hadoop.fs.s3a.audit.execution.interceptors={_INTERCEPTOR_CLASS}",
    # use single-object DELETEs (not the bulk POST ?delete S3Proxy 500s on, and
    # which the interceptor can't cleanly fix up — its response carries a body),
    # so marker deletes go through the DELETE-500 -> 204 path above.
    "spark.hadoop.fs.s3a.multiobjectdelete.enable=false",
    # s3:// and s3a:// both go to oblako's S3 (on AWS Glue, s3:// is EMRFS), so a
    # job script reads and writes plain s3:// paths with no endpoint of its own
    "spark.hadoop.fs.s3.impl=org.apache.hadoop.fs.s3a.S3AFileSystem",
    "spark.hadoop.fs.s3a.impl=org.apache.hadoop.fs.s3a.S3AFileSystem",
    f"spark.hadoop.fs.s3a.endpoint=http://host.docker.internal:{ports.S3}",
    "spark.hadoop.fs.s3a.endpoint.region=us-east-1",
    "spark.hadoop.fs.s3a.path.style.access=true",
    "spark.hadoop.fs.s3a.connection.ssl.enabled=false",
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
        """Build (once, cached) the S3Proxy-compat interceptor jar.

        Compiled in the Glue image itself (it ships javac 17 + the AWS SDK), so
        no host Java toolchain is needed. The cache filename includes a hash of
        the sources, so editing the interceptor transparently triggers a rebuild.
        Returns the host path to the jar, which ``submit_job`` mounts onto the
        Spark classpath.
        """
        import hashlib

        src_dir = Path(__file__).parent / "glue_assets"
        sources = sorted(src_dir.glob("*.java"))
        digest = hashlib.sha256(b"".join(s.read_bytes() for s in sources)).hexdigest()
        jar = (
            Path(tempfile.gettempdir())
            / "oblako-glue"
            / f"oblako-glue-s3a-{digest[:12]}.jar"
        )
        if jar.exists():
            return jar
        jar.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_image()
        build = (
            'set -e; BUNDLE=$(find /root/.m2 -name "bundle-*.jar" | head -1); '
            "mkdir -p /tmp/c && "
            'javac -cp "$BUNDLE" -d /tmp/c /src/*.java && '
            f"(cd /tmp/c && jar cf /out/{jar.name} io)"
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
        """Run a PySpark script in a Glue 5 container.

        Returns ``{exit_code, logs, stdout, stderr}``: ``logs`` is both streams
        together, and the split matches Glue's two CloudWatch log groups (the
        script's own output in ``/aws-glue/jobs/output``, Spark's in ``/error``).

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
            # the AWS SDKs in the container (Glue's Java SDK v2 for from_catalog /
            # getSink catalog updates, boto3 in the script) reach oblako, not AWS
            "AWS_ENDPOINT_URL_GLUE": f"http://host.docker.internal:{ports.GLUE_CATALOG}",
            "AWS_ENDPOINT_URL_S3": f"http://host.docker.internal:{ports.S3}",
            **(env or {}),
        }
        with tempfile.TemporaryDirectory() as scripts_dir:
            (Path(scripts_dir) / "job.py").write_text(script)
            container = self.client.containers.run(
                IMAGE_TAG,
                command=[
                    "spark-submit",
                    *[arg for conf in _SPARK_CONFS for arg in ("--conf", conf)],
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
                stdout = container.logs(stdout=True, stderr=False)
                stderr = container.logs(stdout=False, stderr=True)
                return {
                    "exit_code": result["StatusCode"],
                    "logs": logs,
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": stderr.decode("utf-8", errors="replace"),
                }
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
            except Exception as e:  # surface the failure in the step
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
