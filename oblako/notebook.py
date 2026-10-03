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

from oblako import ports


def _local(port: int) -> str:
    return f"http://localhost:{port}"


# botocore honors AWS_ENDPOINT_URL_<serviceId>; these names are verified to
# resolve. Ports come from the single registry (oblako.ports) so they can't
# drift from the Service defaults / docker-compose.
ENDPOINTS = {
    "AWS_ENDPOINT_URL_S3": _local(ports.S3),
    # the proxy adds vector search and tags; Streams talk to DynamoDB Local
    "AWS_ENDPOINT_URL_DYNAMODB": _local(ports.DYNAMODB_VECTORS),
    "AWS_ENDPOINT_URL_DYNAMODB_STREAMS": _local(ports.DYNAMODB),
    "AWS_ENDPOINT_URL_CLOUDFORMATION": _local(ports.CLOUDFORMATION),
    "AWS_ENDPOINT_URL_SFN": _local(ports.STEPFUNCTIONS),
    "AWS_ENDPOINT_URL_REDSHIFT": _local(ports.REDSHIFT_CONTROL),
    "AWS_ENDPOINT_URL_REDSHIFT_SERVERLESS": _local(ports.REDSHIFT_CONTROL),
    "AWS_ENDPOINT_URL_REDSHIFT_DATA": _local(ports.REDSHIFT_DATA),
    "AWS_ENDPOINT_URL_MWAA": _local(ports.MWAA),
    "AWS_ENDPOINT_URL_SAGEMAKER": _local(ports.SAGEMAKER),
    "AWS_ENDPOINT_URL_SAGEMAKER_RUNTIME": _local(ports.SAGEMAKER),
    "AWS_ENDPOINT_URL_SAGEMAKER_FEATURESTORE_RUNTIME": _local(ports.SAGEMAKER),
    "AWS_ENDPOINT_URL_RDS": _local(ports.RDS_CONTROL),
    "AWS_ENDPOINT_URL_RDS_DATA": _local(ports.RDS_DATA),
    "AWS_ENDPOINT_URL_LAMBDA": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_IAM": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_API_GATEWAY": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_SNS": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_SQS": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_STS": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_SECRETS_MANAGER": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_SSM": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_KMS": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_CLOUDWATCH_LOGS": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_CLOUDWATCH": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_EVENTBRIDGE": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_ECR": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_ECS": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_EKS": _local(ports.MOTO),
    "AWS_ENDPOINT_URL_FIREHOSE": _local(ports.FIREHOSE),
    "AWS_ENDPOINT_URL_KINESIS": _local(ports.KINESIS),
    "AWS_ENDPOINT_URL_ATHENA": _local(ports.ATHENA),
    "AWS_ENDPOINT_URL_GLUE": _local(ports.GLUE_CATALOG),
    "AWS_ENDPOINT_URL_S3VECTORS": _local(ports.S3_VECTORS),
    "AWS_ENDPOINT_URL_S3TABLES": _local(ports.S3_TABLES),
    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": _local(ports.BEDROCK_RUNTIME),
    "AWS_ENDPOINT_URL_BEDROCK": _local(ports.BEDROCK_RUNTIME),
    "AWS_ENDPOINT_URL_APPCONFIG": _local(ports.APPCONFIG),
    "AWS_ENDPOINT_URL_APPCONFIGDATA": _local(ports.APPCONFIG),
    # SageMaker-managed MLflow: the sagemaker-mlflow plugin resolves an
    # `arn:aws:sagemaker:...:mlflow-tracking-server/...` URI to this endpoint, so
    # `mlflow.set_tracking_uri(arn)` reaches the local MLflow container (no
    # SageMaker control plane needed). Faithful to the real ARN-based flow.
    "SAGEMAKER_MLFLOW_CUSTOM_ENDPOINT": _local(ports.MLFLOW),
    # PyIceberg reads catalog settings from PYICEBERG_CATALOG__<NAME>__<KEY>, so a
    # catalog named "s3tables", configured as for AWS (warehouse = table bucket
    # ARN, SigV4), finds oblako's S3 Tables Iceberg endpoint without a uri in code.
    "PYICEBERG_CATALOG__S3TABLES__URI": _local(ports.S3_TABLES) + "/iceberg",
}

_AWS_CONFIG = (
    "[default]\n"
    "region = us-east-1\n"
    "request_checksum_calculation = when_required\n"
    "response_checksum_validation = when_required\n"
    "s3 =\n"
    "    addressing_style = path\n"  # S3Proxy needs path-style (no env knob for it)
)


def _workspace() -> Path:
    """Return the dedicated notebooks workspace directory.

    JupyterLab roots here so it shows only relevant notebooks, not the whole repo;
    override with $OBLAKO_NOTEBOOK_DIR.
    """
    path = Path(
        os.environ.get("OBLAKO_NOTEBOOK_DIR") or Path.home() / ".oblako" / "notebooks"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    return {
        "cell_type": "code",
        "metadata": {},
        "execution_count": None,
        "outputs": [],
        "source": "\n".join(lines),
    }


def _md(*lines: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": "\n".join(lines)}


def starter_notebook() -> dict:
    """Build the welcome notebook (nbformat v4) demonstrating the pre-wired kernel."""
    cells = [
        _md(
            "# oblako — work against your local AWS",
            "",
            "This kernel is **pre-wired**: unmodified `boto3` calls hit oblako's local",
            "services (via `AWS_ENDPOINT_URL_*`), the way code written for real AWS runs.",
            "",
            "Start the services first: `oblako up` (S3, DynamoDB, moto, Step Functions).",
        ),
        _md("## Unmodified boto3 — no `endpoint_url` needed"),
        _code(
            "import boto3",
            "",
            's3 = boto3.client("s3")            # transparently points at S3Proxy',
            's3.create_bucket(Bucket="from-notebook")',
            's3.put_object(Bucket="from-notebook", Key="hello.txt", Body=b"hi from a notebook")',
            'print([b["Name"] for b in s3.list_buckets()["Buckets"]])',
        ),
        _code(
            'ddb = boto3.client("dynamodb")     # DynamoDB Local',
            'print("tables:", ddb.list_tables()["TableNames"])',
        ),
        _md(
            "## oblako helpers — start the in-process servers on first use",
            "",
            "CloudFormation, redshift-data, rds-data and bedrock-runtime are in-process",
            "servers; the `oblako` service objects start them for you.",
        ),
        _code(
            "from oblako.services import Oblako",
            "oblako = Oblako()",
            "",
            "cfn = oblako.cloudformation.get_client()   # auto-starts the local CloudFormation server",
            'print("stacks:", [s["StackName"] for s in cfn.describe_stacks()["Stacks"]])',
        ),
        _md(
            "From here, write the AWS code you normally would — it runs against oblako.",
            "See `examples/` in the repo for S3, DynamoDB, Step Functions, Redshift ML,",
            "CloudFormation, Bedrock, and more.",
        ),
    ]
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            }
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def write_starter(workdir: Path) -> Path:
    """Write the welcome notebook into workdir if it is not already there."""
    path = workdir / "oblako-welcome.ipynb"
    if not path.exists():
        path.write_text(json.dumps(starter_notebook(), indent=1))
    return path


def seed_workspace(workdir: Path) -> Path:
    """Seed the workspace with the welcome notebook + the example notebooks.

    The workspace (``~/.oblako/notebooks``, JupyterLab's root) is oblako's analog
    of a SageMaker notebook instance's EBS-backed home (``/home/ec2-user/SageMaker``):
    persistent, and where the example notebooks land. The repo source for those is
    ``examples/demo-notebooks`` (the Jupyter examples, vs. the ``.py`` CLI scripts
    under ``examples/python``).
    """
    import shutil

    write_starter(workdir)
    examples = Path(__file__).resolve().parents[1] / "examples" / "demo-notebooks"
    dst = workdir / "examples"
    if examples.exists() and not dst.exists():
        shutil.copytree(
            examples,
            dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".aws-sam"),
        )
    return workdir


def launch(port: int = 8888, workdir: Path | None = None) -> int:
    """Launch JupyterLab (blocking) with the oblako-wired environment."""
    workdir = Path(workdir) if workdir else _workspace()
    workdir.mkdir(parents=True, exist_ok=True)
    seed_workspace(workdir)
    env = make_env(workdir)
    return subprocess.run(
        [sys.executable, "-m", "jupyterlab", "--port", str(port)],
        cwd=str(workdir),
        env=env,
        check=False,
    ).returncode


def is_running(port: int = 8888, timeout: float = 0.5) -> bool:
    """Return True if a JupyterLab server is reachable on the port."""
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/api", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def spawn(port: int = 8888, token: str = "oblako", workdir: Path | None = None) -> dict:
    """Start JupyterLab in the background (non-blocking) and return its tokened URL.

    Used by the dashboard's launch button; idempotent per port. A fixed token
    (via JUPYTER_TOKEN) lets the dashboard hand back a directly-openable URL.
    """
    import time

    workdir = Path(workdir) if workdir else _workspace()
    workdir.mkdir(parents=True, exist_ok=True)
    seed_workspace(workdir)
    url = f"http://localhost:{port}/lab?token={token}"
    if is_running(port):
        return {"url": url, "port": port, "already_running": True}
    env = make_env(workdir)
    env["JUPYTER_TOKEN"] = token
    # Allow the dashboard to embed JupyterLab in an iframe: jupyter-server gates
    # framing via the CSP frame-ancestors directive (overridable). XSRF is disabled
    # because a cross-origin iframe can't carry jupyter's XSRF cookie (token auth
    # still applies). Localhost-only, so this is fine for local dev.
    csp = "frame-ancestors 'self' http://localhost:8000 http://127.0.0.1:8000"
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "jupyterlab",
            "--port",
            str(port),
            "--no-browser",
            "--ServerApp.ip",
            "127.0.0.1",
            "--ServerApp.disable_check_xsrf=True",
            '--ServerApp.tornado_settings={"headers": {"Content-Security-Policy": %r}}'
            % csp,
        ],
        cwd=str(workdir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if is_running(port):
            break
        time.sleep(0.3)
    return {"url": url, "port": port, "already_running": False}
