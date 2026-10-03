"""Run an in-process engine as a background service: ``oblako up s3vectors``.

Engines like S3 Vectors, S3 Tables or Athena are Python servers, not containers.
A client of oblako's Python API starts them on demand (``start_in_thread``), but a
reader pointing plain boto3 or the AWS CLI at the endpoint needs them already
listening. ``start`` launches one in a detached process on its canonical port
(``python -m oblako.engines.host <engine> <port>``), records its pid under
``~/.oblako/run`` and its output under ``~/.oblako/logs``, and returns once the
engine answers as itself (see ``identity``). ``stop`` and ``status`` manage it.
"""

from __future__ import annotations

import importlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from oblako import ports
from oblako.engines.identity import is_engine

STATE = Path.home() / ".oblako"

# `oblako up <name>` -> (engine package under oblako.engines, canonical port)
ENGINES: dict[str, tuple[str, int]] = {
    "s3vectors": ("s3vectors", ports.S3_VECTORS),
    "s3tables": ("s3tables", ports.S3_TABLES),
    "athena": ("athena", ports.ATHENA),
    "firehose": ("firehose", ports.FIREHOSE),
    "eventbridge": ("eventbridge", ports.EVENTBRIDGE),
    "appconfig": ("appconfig", ports.APPCONFIG),
    "sagemaker": ("sagemaker", ports.SAGEMAKER),
    "glue": ("glue_catalog", ports.GLUE_CATALOG),
    "dynamodb-vectors": ("dynamodb_vectors", ports.DYNAMODB_VECTORS),
    "redshift-data": ("redshift_data", ports.REDSHIFT_DATA),
    "rds-data": ("rds_data", ports.RDS_DATA),
    # per-instance PostgreSQL behind the RDS API; `oblako up rds` starts it
    "rds-control": ("rds_control", ports.RDS_CONTROL),
    # multi-node clusters behind the Redshift API; `oblako up redshift` starts it
    "redshift-control": ("redshift_control", ports.REDSHIFT_CONTROL),
    "mwaa": ("mwaa", ports.MWAA),
    # RunTask as real containers; the profile's ecs endpoint points here
    "ecs": ("ecs_control", ports.ECS_CONTROL),
    "bedrock-runtime": ("bedrock_runtime", ports.BEDROCK_RUNTIME),
    "cloudformation": ("cloudformation", ports.CLOUDFORMATION),
    "ecs-metadata": ("ecs_metadata", ports.ECS_METADATA),
    # tagging + Inventory behind :9000; `oblako up s3` starts it with S3Proxy
    "s3-ext": ("s3_ext", ports.S3_EXT),
}


def _pidfile(name: str) -> Path:
    return STATE / "run" / f"{name}.pid"


def logfile(name: str) -> Path:
    """Return the log file a background engine writes to."""
    return STATE / "logs" / f"{name}.log"


def is_running(name: str) -> bool:
    """Return True if the engine answers on its port (whoever started it)."""
    engine, port = ENGINES[name]
    return is_engine(port, engine)


def start(name: str, timeout: float = 30.0) -> str:
    """Start the engine in a detached process (idempotent); return its URL."""
    engine, port = ENGINES[name]
    url = f"http://localhost:{port}"
    if is_running(name):
        return url
    for path in (_pidfile(name), logfile(name)):
        path.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile(name), "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "oblako.engines.host", engine, str(port)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # survives the shell that ran `oblako up`
        )
    _pidfile(name).write_text(str(proc.pid))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running(name):
            return url
        if proc.poll() is not None:  # exited: e.g. the port is taken
            break
        time.sleep(0.2)
    stop(name)
    lines = logfile(name).read_text(errors="replace").strip().splitlines()
    reason = lines[-1] if lines else "no output"
    raise RuntimeError(
        f"{name} did not start on port {port}: {reason} (log: {logfile(name)})"
    )


def stop(name: str) -> bool:
    """Stop an engine started by :func:`start`; return False if none was running."""
    pidfile = _pidfile(name)
    try:
        pid = int(pidfile.read_text())
    except (FileNotFoundError, ValueError):
        return False
    pidfile.unlink(missing_ok=True)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


def status(name: str) -> str:
    """Return "running" if the engine answers on its port, else "stopped"."""
    return "running" if is_running(name) else "stopped"


def serve(engine: str, port: int) -> None:
    """Run ``oblako.engines.<engine>`` on ``port`` until it is terminated."""
    module = importlib.import_module(f"oblako.engines.{engine}")
    url = module.start_in_thread(port=port)
    print(f"oblako {engine}: serving on {url}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    threading.Event().wait()  # the server runs in a daemon thread


if __name__ == "__main__":
    serve(sys.argv[1], int(sys.argv[2]))
