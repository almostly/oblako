"""Unit tests for the helpers baked into the Glue Python shell image."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from botocore.session import Session

SITE = Path(__file__).parents[2] / "oblako/services/glue_assets/pythonshell/site"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_get_resolved_options():
    utils = _load("glue_shell_utils", SITE / "awsglue/utils.py")
    argv = ["job.py", "--JOB_NAME", "etl", "--day", "2026-11-06", "--other", "x"]
    assert utils.getResolvedOptions(argv, ["JOB_NAME", "day"]) == {
        "JOB_NAME": "etl",
        "day": "2026-11-06",
    }
    with pytest.raises(SystemExit):
        utils.getResolvedOptions(argv, ["missing"])
    with pytest.raises(SystemExit):
        utils.getResolvedOptions(["job.py", "--dayX", "1"], ["day"])


def test_clients_get_endpoints_from_the_environment(monkeypatch, tmp_path):
    # the hook patches botocore when imported; monkeypatch puts it back after
    monkeypatch.setattr(Session, "create_client", Session.create_client)
    aiobotocore = sys.modules.get("aiobotocore.session")
    if aiobotocore is not None:
        monkeypatch.setattr(
            aiobotocore.AioSession,
            "_create_client",
            aiobotocore.AioSession._create_client,
        )
    _load("glue_shell_sitecustomize", SITE / "sitecustomize.py")
    monkeypatch.setenv("AWS_ENDPOINT_URL_SECRETS_MANAGER", "http://oblako:5000")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    session = Session()
    secrets = session.create_client("secretsmanager", region_name="us-east-1")
    assert secrets.meta.endpoint_url == "http://oblako:5000"
    explicit = session.create_client(
        "secretsmanager", region_name="us-east-1", endpoint_url="http://elsewhere:1"
    )
    assert explicit.meta.endpoint_url == "http://elsewhere:1"
