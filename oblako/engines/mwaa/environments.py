"""Run each MWAA environment as AWS's own Airflow containers.

An environment is the containers Amazon MWAA runs, from the images AWS publishes
as source (github.com/aws/amazon-mwaa-docker-images, Apache-2.0), on a network of
its own:

* ``postgres``: Airflow's metadata database;
* ``sqs``: ElasticMQ, the SQS-compatible queue the Celery executor uses;
* ``migrate-db``: a one-off container that creates the schema;
* ``webserver``, ``scheduler`` and ``worker``: Airflow itself.

The image is built on first use from a pinned commit of that repository, as AWS's
``run.sh`` builds it. DAGs, ``requirements.txt``, the plugins zip and the startup
script come from the environment's S3 source bucket, as on MWAA: DAG files are
synced continually, the others when the environment is created or updated.
Tasks reach oblako's services through the same ``AWS_ENDPOINT_URL_*`` settings
as ``oblako notebook``.

State lives in ``~/.oblako/mwaa/environments.json``; each environment's synced
files are under ``~/.oblako/mwaa/<name>/``.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from oblako import config, ports

HOME = Path.home() / ".oblako" / "mwaa"
STATE = HOME / "environments.json"
SOURCE_REPO = "https://github.com/aws/amazon-mwaa-docker-images.git"
SOURCE_COMMIT = "3b31e1e62e02d53c4a1c2b88fbf624f545cf4537"
AIRFLOW_VERSIONS = (
    "2.9.2",
    "2.10.1",
    "2.10.3",
    "2.11.0",
    "2.11.2",
    "3.0.6",
    "3.2.1",
    "3.3.1",
)
DEFAULT_AIRFLOW_VERSION = "3.3.1"
POSTGRES_IMAGE = "postgres:13"
SQS_IMAGE = "softwaremill/elasticmq:latest"
AIRFLOW_HOME = "/usr/local/airflow"
ROLES = ("webserver", "scheduler", "worker")
LOG_COMPONENTS = (
    "DAGPROCESSOR",
    "SCHEDULER",
    "TASK",
    "TRIGGERER",
    "WEBSERVER",
    "WORKER",
)
SYNC_SECONDS = 10
_lock = threading.RLock()


class MwaaError(Exception):
    """An API error, with AWS's exception name as its code and an HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400):
        """Keep AWS's exception name, its message and the HTTP status."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load() -> dict[str, dict]:
    """Return every environment record, keyed by name."""
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {}


def get(name: str) -> dict | None:
    """Return one environment record, or None."""
    return load().get(name)


def _save(records: dict[str, dict]) -> None:
    """Write all environment records to the state file atomically."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=1, default=str))
    tmp.replace(STATE)


def _update(name: str, fields: dict, *, create: bool = False) -> dict:
    """Merge ``fields`` into a record; only ``create`` adds one (see clusters)."""
    with _lock:
        records = load()
        if name not in records and not create:
            return {}
        record = {**records.get(name, {}), **fields}
        records[name] = record
        _save(records)
        return record


def set_tags(name: str, tags: dict[str, str]) -> None:
    """Replace an environment's tags."""
    _update(name, {"Tags": tags})


def _drop(name: str) -> None:
    """Remove an environment's record."""
    with _lock:
        records = load()
        records.pop(name, None)
        _save(records)


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def arn(name: str) -> str:
    """Return the environment's ARN in oblako's account and region."""
    return f"arn:aws:airflow:{config.region()}:{config.account_id()}:environment/{name}"


def files_dir(name: str) -> Path:
    """Return the host folder that holds an environment's synced files."""
    return HOME / name


# ---------------------------------------------------------------------------
# Docker plumbing
# ---------------------------------------------------------------------------
def _docker():
    """Return a Docker client."""
    from oblako.services.backends import docker_client

    return docker_client()


def container_name(name: str, role: str) -> str:
    """Return the container name of one of an environment's roles."""
    return f"oblako-mwaa-{name}-{role}"


def _network_name(name: str) -> str:
    """Return the name of an environment's Docker network."""
    return f"oblako-mwaa-{name}"


def image(version: str) -> str:
    """Return the local tag of the Airflow image for ``version``."""
    return os.environ.get("OBLAKO_MWAA_IMAGE") or f"oblako/mwaa-airflow:{version}"


def _free_port() -> int:
    """Return a free TCP port on the host."""
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def ensure_image(version: str) -> str:
    """Use the local Airflow image, else build it from AWS's sources."""
    from docker.errors import ImageNotFound

    client = _docker()
    tag = image(version)
    try:
        client.images.get(tag)
        return tag
    except ImageNotFound:
        pass
    source = HOME / "src"
    if not (source / ".git").exists():
        shutil.rmtree(source, ignore_errors=True)
        subprocess.run(
            ["git", "clone", "--quiet", SOURCE_REPO, str(source)], check=True
        )
    subprocess.run(
        ["git", "-C", str(source), "checkout", "--quiet", SOURCE_COMMIT], check=True
    )
    context = source / "images" / "airflow" / version
    # the derived Dockerfile builds FROM this tag of the base image
    base = f"localhost/amazon-mwaa-docker-images/airflow:{version}-base"
    for dockerfile, built in (("Dockerfile.base", base), ("Dockerfile", tag)):
        subprocess.run(
            [
                "docker",
                "build",
                "--quiet",
                "-f",
                str(context / "Dockerfiles" / dockerfile),
                "-t",
                built,
                str(context),
            ],
            check=True,
            capture_output=True,
        )
    return tag


def _service_endpoints() -> dict[str, str]:
    """Return the notebook's AWS_ENDPOINT_URL_* settings, as seen from a container."""
    from oblako.notebook import ENDPOINTS

    return {
        key: value.replace("localhost", "host.docker.internal")
        for key, value in ENDPOINTS.items()
    }


def _airflow_env(name: str, record: dict) -> dict[str, str]:
    """Environment variables for the Airflow containers (AWS's compose file)."""
    custom = record.get("AirflowConfigurationOptions") or {}
    env = {
        **_service_endpoints(),
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_REGION": config.region(),
        "AWS_DEFAULT_REGION": config.region(),
        "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
        "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
        "MWAA__CORE__AUTH_TYPE": "testing",
        "MWAA__CORE__CUSTOM_AIRFLOW_CONFIGS": json.dumps(custom),
        "MWAA__CORE__FERNET_KEY": record["fernet_key"],
        "MWAA__WEBSERVER__SECRET": json.dumps({"secret_key": record["fernet_key"]}),
        "MWAA__DB__CREDENTIALS": json.dumps(
            {"username": "airflow", "password": "airflow"}
        ),
        "MWAA__DB__POSTGRES_DB": "airflow",
        "MWAA__DB__POSTGRES_HOST": "postgres",
        "MWAA__DB__POSTGRES_PORT": "5432",
        "MWAA__DB__POSTGRES_SSLMODE": "prefer",
        "MWAA__SQS__CREATE_QUEUE": "True",
        "MWAA__SQS__CUSTOM_ENDPOINT": "http://sqs:9324",
        "MWAA__SQS__QUEUE_URL": "http://sqs:9324/000000000000/celery-queue",
        "MWAA__SQS__USE_SSL": "False",
    }
    # as AWS's run.sh sets them: the entrypoint skips files that are absent
    env["MWAA__CORE__REQUIREMENTS_PATH"] = (
        f"{AIRFLOW_HOME}/requirements/requirements.txt"
    )
    env["MWAA__CORE__STARTUP_SCRIPT_PATH"] = f"{AIRFLOW_HOME}/startup/startup.sh"
    # Airflow 3 workers run tasks through the API server
    env["MWAA__CORE__API_SERVER_URL"] = "http://webserver:8080"
    env["MWAA_LOCAL_RUNNER"] = "true"
    for flag in ("TASK_MONITORING", "TERMINATE_IF_IDLE", "MWAA_SIGNAL_HANDLING"):
        suffix = "" if flag == "TERMINATE_IF_IDLE" else "_ENABLED"
        env[f"MWAA__CORE__{flag}{suffix}"] = "false"
    # the entrypoint reads every component's logging settings; CloudWatch is off
    for component in LOG_COMPONENTS:
        prefix = f"MWAA__LOGGING__AIRFLOW_{component}"
        env[f"{prefix}_LOGS_ENABLED"] = "false"
        env[f"{prefix}_LOG_GROUP_ARN"] = ""
        env[f"{prefix}_LOG_LEVEL"] = "INFO"
    return env


def _volumes(name: str) -> dict[str, dict]:
    """Return the bind mounts of an environment's synced folders into Airflow."""
    root = files_dir(name)
    return {
        str(root / folder): {"bind": f"{AIRFLOW_HOME}/{folder}", "mode": "rw"}
        for folder in ("dags", "plugins", "requirements", "startup")
    }


def _remove_container(client, container: str) -> None:
    """Force-remove a container, if it exists."""
    from docker.errors import NotFound

    with contextlib.suppress(NotFound):
        client.containers.get(container).remove(force=True)


def _start_backing_services(client, name: str) -> None:
    """Start the environment's PostgreSQL and ElasticMQ and wait for both."""
    network = _network_name(name)
    _remove_container(client, container_name(name, "postgres"))
    client.containers.run(
        POSTGRES_IMAGE,
        name=container_name(name, "postgres"),
        detach=True,
        network=network,
        hostname="postgres",
        environment={
            "POSTGRES_USER": "airflow",
            "POSTGRES_PASSWORD": "airflow",
            "POSTGRES_DB": "airflow",
        },
        volumes={f"oblako-mwaa-{name}-db": {"bind": "/var/lib/postgresql/data"}},
        labels={"oblako.mwaa": name},
    )
    _remove_container(client, container_name(name, "sqs"))
    client.containers.run(
        SQS_IMAGE,
        name=container_name(name, "sqs"),
        detach=True,
        network=network,
        hostname="sqs",
        labels={"oblako.mwaa": name},
    )
    postgres = client.containers.get(container_name(name, "postgres"))
    deadline = time.time() + 120
    while time.time() < deadline:
        if postgres.exec_run("pg_isready -U airflow").exit_code == 0:
            return
        time.sleep(1)
    raise RuntimeError("the environment's PostgreSQL did not become ready")


def _start_airflow(client, name: str, record: dict) -> None:
    """Run the schema migration, then the webserver, scheduler and worker."""
    tag = image(record["AirflowVersion"])
    network = _network_name(name)
    env = _airflow_env(name, record)
    common = {
        "environment": env,
        "network": network,
        "volumes": _volumes(name),
        "extra_hosts": {"host.docker.internal": "host-gateway"},
        "labels": {"oblako.mwaa": name},
        "init": True,
    }
    _remove_container(client, container_name(name, "migrate-db"))
    migrate = client.containers.run(
        tag,
        "migrate-db",
        name=container_name(name, "migrate-db"),
        detach=True,
        **common,
    )
    if migrate.wait(timeout=900)["StatusCode"] != 0:
        tail = migrate.logs(tail=20).decode(errors="replace")
        raise RuntimeError(f"migrate-db failed:\n{tail}")
    for role in ROLES:
        _remove_container(client, container_name(name, role))
        extra = {}
        if role == "webserver":
            extra["ports"] = {"8080/tcp": ("127.0.0.1", record["port"])}
            extra["hostname"] = "webserver"
        client.containers.run(
            tag,
            role,
            name=container_name(name, role),
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            **common,
            **extra,
        )


def _wait_for_webserver(record: dict, timeout: float = 900) -> None:
    """Wait until the webserver answers its health endpoint."""
    url = f"http://localhost:{record['port']}"
    paths = ("/api/v2/monitor/health", "/health")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for path in paths:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(url + path, timeout=3).status_code == 200:
                    return
        time.sleep(3)
    raise RuntimeError("the Airflow webserver did not become healthy")


def _stop(client, name: str) -> None:
    """Force-remove every container labeled with the environment."""
    for container in client.containers.list(
        all=True, filters={"label": f"oblako.mwaa={name}"}
    ):
        container.remove(force=True)


# ---------------------------------------------------------------------------
# Files from S3
# ---------------------------------------------------------------------------
def _s3():
    """Return an S3 client for oblako's S3."""
    from oblako.services import boto

    return boto.client("s3", f"http://localhost:{ports.S3}")


def _bucket(record: dict) -> str:
    """Return the bucket name from the record's SourceBucketArn."""
    return record["SourceBucketArn"].split(":::", 1)[-1]


def sync_dags(name: str, record: dict) -> int:
    """Mirror the DAG folder from S3 into the environment; return the file count."""
    s3 = _s3()
    prefix = record["DagS3Path"].rstrip("/") + "/"
    target = files_dir(name) / "dags"
    target.mkdir(parents=True, exist_ok=True)
    wanted: dict[Path, str] = {}
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=_bucket(record), Prefix=prefix
    )
    for page in pages:
        for obj in page.get("Contents", []):
            relative = obj["Key"][len(prefix) :]
            if relative and not relative.endswith("/"):
                wanted[target / relative] = obj["ETag"]
    seen = record.get("dag_etags", {})
    for path, etag in wanted.items():
        if seen.get(str(path)) != etag or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            key = prefix + str(path.relative_to(target))
            body = s3.get_object(Bucket=_bucket(record), Key=key)["Body"].read()
            path.write_bytes(body)
    for path in target.rglob("*"):
        if path.is_file() and path not in wanted:
            path.unlink()
    _update(name, {"dag_etags": {str(p): e for p, e in wanted.items()}})
    return len(wanted)


def _fetch_files(name: str, record: dict) -> None:
    """Fetch requirements, plugins and the startup script (create and update)."""
    s3 = _s3()
    root = files_dir(name)
    for folder in ("dags", "plugins", "requirements", "startup"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    def read(key: str, version: str | None) -> bytes:
        """Return an S3 object's bytes (at ``version`` if given), else raise MwaaError."""
        kwargs = {"VersionId": version} if version else {}
        try:
            return s3.get_object(Bucket=_bucket(record), Key=key, **kwargs)[
                "Body"
            ].read()
        except Exception as e:
            raise MwaaError("ValidationException", f"s3://{_bucket(record)}/{key}: {e}")

    if record.get("RequirementsS3Path"):
        body = read(
            record["RequirementsS3Path"], record.get("RequirementsS3ObjectVersion")
        )
        (root / "requirements" / "requirements.txt").write_bytes(body)
    shutil.rmtree(root / "plugins")
    (root / "plugins").mkdir()
    if record.get("PluginsS3Path"):
        body = read(record["PluginsS3Path"], record.get("PluginsS3ObjectVersion"))
        zipfile.ZipFile(io.BytesIO(body)).extractall(root / "plugins")
    if record.get("StartupScriptS3Path"):
        body = read(
            record["StartupScriptS3Path"], record.get("StartupScriptS3ObjectVersion")
        )
        script = root / "startup" / "startup.sh"
        script.write_bytes(body)
        script.chmod(0o755)
    sync_dags(name, record)


def sync_loop(stop: threading.Event) -> None:
    """Sync every available environment's DAGs from S3 until ``stop`` is set."""
    while not stop.wait(SYNC_SECONDS):
        for name, record in load().items():
            if record.get("Status") == "AVAILABLE":
                with contextlib.suppress(Exception):
                    sync_dags(name, record)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def defaults(name: str, req: dict) -> dict:
    """Return the settings AWS reports for an environment created without them.

    Observed on MWAA (mw1.micro, Airflow 3.3.1): one worker, scheduler and
    webserver for mw1.micro, task logs on and the others off, service-managed
    endpoints. oblako records and reports these; it does not enforce them.
    """
    micro = req.get("EnvironmentClass", "mw1.small") == "mw1.micro"
    region, account = config.region(), config.account_id()
    log_group = f"arn:aws:logs:{region}:{account}:log-group:airflow-{name}"
    logs = {
        kind: {"Enabled": False, "LogLevel": "INFO"}
        for kind in (
            "DagProcessingLogs",
            "SchedulerLogs",
            "WebserverLogs",
            "WorkerLogs",
        )
    }
    logs["TaskLogs"] = {
        "Enabled": True,
        "LogLevel": "INFO",
        "CloudWatchLogGroupArn": f"{log_group}-Task",
    }
    return {
        "EnvironmentClass": "mw1.small",
        "MinWorkers": 1,
        "MaxWorkers": 1 if micro else 10,
        "Schedulers": 1 if micro else 2,
        "MinWebservers": 1 if micro else 2,
        "MaxWebservers": 1 if micro else 2,
        "WebserverAccessMode": "PRIVATE_ONLY",
        "EndpointManagement": "SERVICE",
        "WeeklyMaintenanceWindowStart": "SUN:03:00",
        "LoggingConfiguration": logs,
        "ServiceRoleArn": (
            f"arn:aws:iam::{account}:role/aws-service-role/"
            "airflow.amazonaws.com/AWSServiceRoleForAmazonMWAA"
        ),
        "CeleryExecutorQueue": (
            f"arn:aws:sqs:{region}:{account}:airflow-celery-{uuid.uuid4()}"
        ),
        "Tags": {},
    }


def _without_nulls(value):
    """Drop null fields from Airflow's responses, as InvokeRestApi does."""
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


def _in_background(name: str, work, done_status: str = "AVAILABLE") -> None:
    """Run ``work`` in a thread, then set the status (or the failure) on the record."""

    def run():
        """Run the work and record success, or the failure unless being deleted."""
        try:
            work()
            done = {
                "Status": "SUCCESS",
                "CreatedAt": _now(),
                "WorkerReplacementStrategy": "FORCED",
            }
            _update(name, {"Status": done_status, "error": None, "LastUpdate": done})
        except Exception as e:
            failed = "CREATE_FAILED" if done_status == "AVAILABLE" else "UPDATE_FAILED"
            current = get(name)
            if current is not None and current.get("Status") != "DELETING":
                _update(name, {"Status": failed, "error": str(e)})

    threading.Thread(target=run, daemon=True).start()


def create(name: str, req: dict) -> dict:
    """Record a new environment and start its containers in the background."""
    version = req.get("AirflowVersion") or DEFAULT_AIRFLOW_VERSION
    if version not in AIRFLOW_VERSIONS:
        raise MwaaError(
            "ValidationException",
            f"AirflowVersion {version} is not one of {', '.join(AIRFLOW_VERSIONS)}",
        )
    for key in ("SourceBucketArn", "DagS3Path", "ExecutionRoleArn"):
        if not req.get(key):
            raise MwaaError("ValidationException", f"{key} is required")
    with _lock:
        if name in load():
            raise MwaaError("ValidationException", f"Environment {name} already exists")
        port = _free_port()
        fields = {
            **defaults(name, req),
            **req,
            "Name": name,
            "Arn": arn(name),
            "AirflowVersion": version,
            "Status": "CREATING",
            "CreatedAt": _now(),
            "WebserverUrl": f"localhost:{port}",
            "port": port,
            "fernet_key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        }
        record = _update(name, fields, create=True)

    def work():
        """Build the image and network, fetch files, and start the containers."""
        client = _docker()
        ensure_image(version)
        from docker.errors import NotFound

        try:
            client.networks.get(_network_name(name))
        except NotFound:
            client.networks.create(_network_name(name), driver="bridge")
        _fetch_files(name, record)
        _start_backing_services(client, name)
        _start_airflow(client, name, record)
        _wait_for_webserver(record)

    _in_background(name, work)
    return record


def update(name: str, req: dict) -> dict:
    """Apply new settings and files, restarting Airflow, in the background."""
    record = get(name)
    if record is None:
        raise MwaaError(
            "ResourceNotFoundException", f"Environment {name} not found.", 404
        )
    if record["Status"] not in ("AVAILABLE", "UPDATE_FAILED"):
        raise MwaaError(
            "ValidationException",
            f"Environment {name} is {record['Status']} and cannot be updated",
        )
    if req.get("AirflowVersion") and req["AirflowVersion"] != record["AirflowVersion"]:
        raise MwaaError(
            "ValidationException", "oblako does not upgrade an environment's Airflow"
        )
    changes = {
        **{k: v for k, v in req.items() if k != "Name"},
        "Status": "UPDATING",
        "LastUpdate": {"Status": "PENDING", "CreatedAt": _now()},
    }
    record = _update(name, changes)

    def work():
        """Fetch the files and restart Airflow with the new settings."""
        client = _docker()
        _fetch_files(name, record)
        _start_airflow(client, name, record)
        _wait_for_webserver(record)

    _in_background(name, work)
    return record


def delete(name: str) -> None:
    """Remove the environment's containers, network, database and files."""
    from docker.errors import NotFound

    if get(name) is None:
        raise MwaaError(
            "ResourceNotFoundException", f"Environment {name} not found.", 404
        )
    _update(name, {"Status": "DELETING"})
    client = _docker()
    _stop(client, name)
    with contextlib.suppress(NotFound):
        client.networks.get(_network_name(name)).remove()
    with contextlib.suppress(NotFound):
        client.volumes.get(f"oblako-mwaa-{name}-db").remove(force=True)
    shutil.rmtree(files_dir(name), ignore_errors=True)
    _drop(name)


# ---------------------------------------------------------------------------
# Airflow's REST API
# ---------------------------------------------------------------------------
def invoke_rest_api(name: str, req: dict) -> tuple[int, object]:
    """Call Airflow's REST API on the environment's webserver, as InvokeRestApi does.

    The webserver uses Airflow's simple auth manager with every user an admin
    (AWS's ``testing`` auth type), so a token for any user is accepted.
    """
    record = get(name)
    if record is None:
        raise MwaaError(
            "ResourceNotFoundException", f"Environment {name} not found.", 404
        )
    if record["Status"] != "AVAILABLE":
        raise MwaaError(
            "ValidationException", f"Environment {name} is {record['Status']}"
        )
    base = f"http://localhost:{record['port']}"
    major = int(record["AirflowVersion"].split(".")[0])
    headers = {}
    if major >= 3:
        token = httpx.post(
            f"{base}/auth/token",
            json={"username": "admin", "password": "admin"},
            timeout=30,
        )
        token.raise_for_status()
        headers["Authorization"] = f"Bearer {token.json()['access_token']}"
        prefix = "/api/v2"
    else:
        prefix = "/api/v1"
    resp = httpx.request(
        req["Method"],
        base + prefix + req["Path"],
        params=req.get("QueryParameters") or None,
        json=req.get("Body"),
        headers=headers,
        timeout=60,
    )
    try:
        body: object = _without_nulls(resp.json())
    except ValueError:
        body = {"detail": resp.text}
    return resp.status_code, body
