"""Local AWS Glue: Spark jobs and Python shell jobs.

PySpark jobs run in the official ``amazon/aws-glue-libs:5`` image, and Python
shell jobs in a Python 3.9 image with Glue's library sets.

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
from oblako.notebook import ENDPOINTS

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
    # AWS Glue leaves only the data files (checked on Glue 5.0, 2026-10); S3A
    # would also keep a zero-byte marker per partition directory (day=.../),
    # which s3fs-based readers fail on. Delete each marker once files land under it.
    "spark.hadoop.fs.s3a.directory.marker.retention=delete",
)


PYTHON_SHELL_IMAGE = "oblako/glue-pythonshell"
_PYTHON_SHELL_DIR = Path(__file__).parent / "glue_assets" / "pythonshell"


def _container_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a job container's environment: credentials and oblako's endpoints."""
    return {
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": config.region(),
        # S3Proxy doesn't speak the new flexible checksums.
        "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
        "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
        # the AWS SDKs in the container (Glue's Java SDK v2 for from_catalog /
        # getSink catalog updates, boto3 in the script: Secrets Manager, S3,
        # every service) reach oblako, not AWS
        **{
            key: value.replace("localhost", "host.docker.internal")
            for key, value in ENDPOINTS.items()
        },
        **(env or {}),
    }


def _collect(container, timeout: int) -> dict:
    """Wait for a job container, return its exit code and logs, and remove it."""
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
        full_env = _container_env(env)
        # on oblako's RDS network, when there is one, so a job reaches an RDS
        # instance at its endpoint (<id>.<region>.rds.localhost), as on AWS
        try:
            from oblako.engines.rds_control.instances import NETWORK

            network = {"network": self.client.networks.get(NETWORK).name}
        except Exception:
            network = {}
        with tempfile.TemporaryDirectory() as scripts_dir:
            (Path(scripts_dir) / "job.py").write_text(script)
            container = self.client.containers.run(
                IMAGE_TAG,
                command=[
                    "spark-submit",
                    *[arg for conf in _SPARK_CONFS for arg in ("--conf", conf)],
                    # the script, run under Glue's job bookmarks (run_job.py)
                    "/oblako/run_job.py",
                    *(args or []),
                ],
                detach=True,
                volumes={
                    scripts_dir: {"bind": "/scripts", "mode": "ro"},
                    str(Path(__file__).parent / "glue_assets" / "run_job.py"): {
                        "bind": "/oblako/run_job.py",
                        "mode": "ro",
                    },
                    # drop the interceptor into a classpath-glob dir so S3A loads it
                    str(interceptor_jar): {
                        "bind": "/opt/spark/jars/oblako-glue-s3a.jar",
                        "mode": "ro",
                    },
                },
                environment=full_env,
                extra_hosts={"host.docker.internal": "host-gateway"},
                **network,
            )
            return _collect(container, timeout)

    def ensure_python_shell_image(self, library_set: str) -> str:
        """Build the Python shell 3.9 image for a library set, once, and return its tag.

        The tag carries a hash of the image's sources, so a change to the
        Dockerfile, the requirements or the site files builds a new image.
        """
        import hashlib

        from docker.errors import ImageNotFound

        sources = sorted(
            f
            for f in _PYTHON_SHELL_DIR.rglob("*")
            if f.is_file() and "__pycache__" not in f.parts
        )
        digest = hashlib.sha256()
        for source in sources:
            digest.update(source.relative_to(_PYTHON_SHELL_DIR).as_posix().encode())
            digest.update(source.read_bytes())
        tag = f"{PYTHON_SHELL_IMAGE}:3.9-{library_set}-{digest.hexdigest()[:12]}"
        try:
            self.client.images.get(tag)
        except ImageNotFound:
            print(f"Building {tag} (Glue's Python shell libraries)...")
            self.client.images.build(
                path=str(_PYTHON_SHELL_DIR),
                tag=tag,
                buildargs={"LIBRARY_SET": library_set},
                rm=True,
            )
        return tag

    def run_python_shell(
        self,
        script: str,
        *,
        args: list[str] | None = None,
        library_set: str = "analytics",
        python_modules: list[str] | None = None,
        extra_py_files: dict[str, bytes] | None = None,
        timeout: int = 1200,
    ) -> dict:
        """Run a Glue Python shell script: plain Python 3.9, Glue's libraries, no Spark.

        ``library_set`` is Glue's ``library-set`` (``analytics`` or ``none``).
        ``python_modules`` are pip arguments (``--additional-python-modules``),
        installed before the script runs. ``extra_py_files`` are the
        ``--extra-py-files`` by file name, put on ``PYTHONPATH`` (a ``.whl`` or
        ``.zip`` itself, a ``.py`` by its directory). Returns what
        :meth:`submit_job` returns.
        """
        import shlex

        image = self.ensure_python_shell_image(library_set)
        command = f"exec python -u /scripts/job.py {shlex.join(args or [])}"
        if python_modules:
            command = f"pip install --quiet --root-user-action=ignore {shlex.join(python_modules)} && {command}"
        paths = ["/opt/oblako", "/scripts/oblako", "/scripts/lib"]
        paths += [
            f"/scripts/lib/{n}"
            for n in extra_py_files or {}
            if n.endswith((".whl", ".zip"))
        ]
        with tempfile.TemporaryDirectory() as scripts_dir:
            (Path(scripts_dir) / "job.py").write_text(script)
            lib = Path(scripts_dir) / "lib"
            lib.mkdir()
            for file_name, body in (extra_py_files or {}).items():
                (lib / file_name).write_bytes(body)
            # redshift_connector trusts oblako's Redshift, as after `oblako trust`
            from oblako.services.redshift import RedshiftService, write_trust_keeper

            trust = Path(scripts_dir) / "oblako"
            trust.mkdir()
            write_trust_keeper(trust, RedshiftService().server_certs())
            container = self.client.containers.create(
                image,
                command=["sh", "-c", command],
                volumes={scripts_dir: {"bind": "/scripts", "mode": "ro"}},
                environment=_container_env({"PYTHONPATH": ":".join(paths)}),
                extra_hosts={"host.docker.internal": "host-gateway"},
            )
            # on oblako's RDS and Redshift networks, when they exist, so a job
            # reaches an instance or a cluster at its endpoint, as on AWS
            from oblako.engines.rds_control.instances import NETWORK as RDS_NETWORK
            from oblako.engines.redshift_control.clusters import (
                NETWORK as REDSHIFT_NETWORK,
            )
            from docker.errors import NotFound

            for name in (RDS_NETWORK, REDSHIFT_NETWORK):
                try:
                    self.client.networks.get(name).connect(container)
                except NotFound:
                    pass
            container.start()
            return _collect(container, timeout)

    def run_workflow(self, name: str, steps: list[dict], *, timeout: int = 600) -> dict:
        """Run a Glue workflow: a sequential pipeline of PySpark job steps.

        Each step is ``{name, script}``. Steps run in order, and a step runs only
        if every prior step SUCCEEDED — mirroring a Glue CONDITIONAL trigger gated
        on predecessor success. The first failure stops the pipeline; the rest are
        marked SKIPPED. Returns ``{name, status, steps:[{name, status, exitCode,
        logs}]}``.

        This covers the common sequential-ETL workflow from Python. The Glue API
        (``CreateWorkflow``, ``CreateTrigger``, ``StartWorkflowRun``) runs full
        graphs: conditional, scheduled and on-demand triggers, jobs and crawlers.
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
