"""Launch JupyterLab pre-wired to oblako's local services.

Sets `AWS_ENDPOINT_URL_*` (+ test creds, region, S3 path-style/checksum) in the
kernel env so **unmodified** boto3 — `boto3.client("s3")`, no `endpoint_url` —
transparently hits oblako, the way code written for real AWS would run. The
always-on Docker services (S3, DynamoDB, moto control planes, Step Functions)
work immediately after `oblako up`; the in-process servers (CloudFormation,
redshift-data, rds-data, bedrock-runtime) start on first use via the `oblako`
service helpers (e.g. `Oblako().cloudformation.get_client()`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# botocore honors AWS_ENDPOINT_URL_<serviceId>; these names are verified to resolve.
ENDPOINTS = {
    "AWS_ENDPOINT_URL_S3": "http://localhost:9000",
    "AWS_ENDPOINT_URL_DYNAMODB": "http://localhost:8001",
    "AWS_ENDPOINT_URL_CLOUDFORMATION": "http://localhost:5601",
    "AWS_ENDPOINT_URL_SFN": "http://localhost:8083",
    "AWS_ENDPOINT_URL_REDSHIFT": "http://localhost:5500",
    "AWS_ENDPOINT_URL_REDSHIFT_DATA": "http://localhost:8002",
    "AWS_ENDPOINT_URL_RDS": "http://localhost:5500",
    "AWS_ENDPOINT_URL_RDS_DATA": "http://localhost:8006",
    "AWS_ENDPOINT_URL_LAMBDA": "http://localhost:5500",
    "AWS_ENDPOINT_URL_IAM": "http://localhost:5500",
    "AWS_ENDPOINT_URL_API_GATEWAY": "http://localhost:5500",
    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": "http://localhost:8004",
    "AWS_ENDPOINT_URL_BEDROCK": "http://localhost:8004",
}

_AWS_CONFIG = (
    "[default]\n"
    "region = us-east-1\n"
    "request_checksum_calculation = when_required\n"
    "response_checksum_validation = when_required\n"
    "s3 =\n"
    "    addressing_style = path\n"  # S3Proxy needs path-style (no env knob for it)
)


def make_env(workdir: Path) -> dict[str, str]:
    """Return an environment dict that points unmodified boto3 at oblako's services."""
    config = workdir / ".oblako-aws-config"
    config.write_text(_AWS_CONFIG)
    env = dict(os.environ)
    env.update(ENDPOINTS)
    env.update(
        AWS_ACCESS_KEY_ID="test",
        AWS_SECRET_ACCESS_KEY="test",
        AWS_DEFAULT_REGION="us-east-1",
        AWS_REQUEST_CHECKSUM_CALCULATION="when_required",
        AWS_CONFIG_FILE=str(config),
    )
    return env


def _code(*lines: str) -> dict:
    return {"cell_type": "code", "metadata": {}, "execution_count": None,
            "outputs": [], "source": "\n".join(lines)}


def _md(*lines: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": "\n".join(lines)}


def starter_notebook() -> dict:
    """Build the welcome notebook (nbformat v4) demonstrating the pre-wired kernel."""
    cells = [
        _md("# oblako — work against your local AWS",
            "",
            "This kernel is **pre-wired**: unmodified `boto3` calls hit oblako's local",
            "services (via `AWS_ENDPOINT_URL_*`), the way code written for real AWS runs.",
            "",
            "Start the services first: `oblako up` (S3, DynamoDB, moto, Step Functions)."),
        _md("## Unmodified boto3 — no `endpoint_url` needed"),
        _code("import boto3",
              "",
              's3 = boto3.client("s3")            # transparently points at S3Proxy',
              's3.create_bucket(Bucket="from-notebook")',
              's3.put_object(Bucket="from-notebook", Key="hello.txt", Body=b"hi from a notebook")',
              'print([b["Name"] for b in s3.list_buckets()["Buckets"]])'),
        _code('ddb = boto3.client("dynamodb")     # DynamoDB Local',
              'print("tables:", ddb.list_tables()["TableNames"])'),
        _md("## oblako helpers — start the in-process servers on first use",
            "",
            "CloudFormation, redshift-data, rds-data and bedrock-runtime are in-process",
            "servers; the `oblako` service objects start them for you."),
        _code("from oblako.services import Oblako",
              "oblako = Oblako()",
              "",
              "cfn = oblako.cloudformation.get_client()   # auto-starts the local CloudFormation server",
              'print("stacks:", [s["StackName"] for s in cfn.describe_stacks()["Stacks"]])'),
        _md("From here, write the AWS code you normally would — it runs against oblako.",
            "See `examples/` in the repo for S3, DynamoDB, Step Functions, Redshift ML,",
            "CloudFormation, Bedrock, and more."),
    ]
    return {"cells": cells,
            "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
            "nbformat": 4, "nbformat_minor": 5}


def write_starter(workdir: Path) -> Path:
    """Write the welcome notebook into workdir if it is not already there."""
    path = workdir / "oblako-welcome.ipynb"
    if not path.exists():
        path.write_text(json.dumps(starter_notebook(), indent=1))
    return path


def launch(port: int = 8888, workdir: Path | None = None) -> int:
    """Launch JupyterLab (blocking) with the oblako-wired environment."""
    workdir = Path(workdir or Path.cwd())
    workdir.mkdir(parents=True, exist_ok=True)
    write_starter(workdir)
    env = make_env(workdir)
    return subprocess.run(
        [sys.executable, "-m", "jupyterlab", "--port", str(port)],
        cwd=str(workdir), env=env, check=False,
    ).returncode


def is_running(port: int = 8888, timeout: float = 0.5) -> bool:
    """Return True if a JupyterLab server is reachable on the port."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://localhost:{port}/api", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def spawn(port: int = 8888, token: str = "oblako", workdir: Path | None = None) -> dict:
    """Start JupyterLab in the background (non-blocking) and return its tokened URL.

    Used by the dashboard's launch button; idempotent per port. A fixed token
    (via JUPYTER_TOKEN) lets the dashboard hand back a directly-openable URL.
    """
    import time

    workdir = Path(workdir or Path.cwd())
    workdir.mkdir(parents=True, exist_ok=True)
    write_starter(workdir)
    url = f"http://localhost:{port}/lab?token={token}"
    if is_running(port):
        return {"url": url, "port": port, "already_running": True}
    env = make_env(workdir)
    env["JUPYTER_TOKEN"] = token
    subprocess.Popen(
        [sys.executable, "-m", "jupyterlab", "--port", str(port),
         "--no-browser", "--ServerApp.ip", "127.0.0.1"],
        cwd=str(workdir), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if is_running(port):
            break
        time.sleep(0.3)
    return {"url": url, "port": port, "already_running": False}
