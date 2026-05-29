"""Integration tests for AWS Lambda — control plane *and* real execution.

Requires running services:
    docker compose up -d moto   # moto on 5500, with /var/run/docker.sock mounted

Lambda is special: moto stores the function metadata, but the actual handler
runs in a per-invoke container that moto spawns on the host Docker daemon (the
socket is mounted into the moto container by MotoService). So these tests cover
the bits that have repeatedly bitten us:

  - the function executes for real and returns the handler's output,
  - the default architecture is x86_64 (real-AWS default; matters on Apple
    Silicon, where the host is arm64),
  - a layer published as a zip is unpacked onto /opt and importable at runtime.

If Docker exec isn't available (no socket in the moto container, or the daemon
is down), the execution tests skip rather than fail — the control-plane test
still runs.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile

import pytest

from oblako.services.awslambda import LambdaService
from oblako.services.moto import MotoService

RUNTIME = "python3.12"  # AL2023 / glibc 2.34 — matches real AWS, loads modern wheels


def _zip(files: dict[str, str]) -> bytes:
    """Pack {path: text} into an in-memory zip (deployment package / layer)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in files.items():
            z.writestr(path, text)
    return buf.getvalue()


def _invoke(svc, name: str, event: dict):
    """Invoke and return (status, parsed_payload, raw). Skip on Docker-exec errors."""
    resp = svc.get_client().invoke(
        FunctionName=name,
        Payload=json.dumps(event).encode(),
    )
    raw = resp["Payload"].read().decode("utf-8", errors="replace")
    if "error running docker" in raw:
        pytest.skip(f"Docker exec unavailable in moto: {raw[:120]}")
    payload = json.loads(raw) if raw else None
    return resp.get("StatusCode"), payload, raw


@pytest.fixture(scope="module")
def svc():
    """LambdaService wired to a live moto; ensure the exec role + runtime image."""
    moto = MotoService()
    if not moto.wait_ready(timeout=5):
        pytest.skip("moto is not running on :5500")
    lam = LambdaService(moto=moto)
    lam.ensure_exec_role()
    try:
        lam.ensure_runtime_image(RUNTIME, architecture="x86_64")
    except Exception as e:  # noqa: BLE001 — image pull is best-effort
        pytest.skip(f"could not pull the {RUNTIME} runtime image: {e}")
    return lam


@pytest.fixture
def function(svc):
    """Create a throwaway hello-world function; clean it up after."""
    client = svc.get_client()
    name = f"oblako-test-{uuid.uuid4().hex[:8]}"
    source = (
        "def handler(event, context):\n"
        "    return {'ok': True, 'echo': event, 'who': 'oblako'}\n"
    )
    client.create_function(
        FunctionName=name,
        Runtime=RUNTIME,
        Role=svc.ensure_exec_role(),
        Handler="lambda_function.handler",
        Code={"ZipFile": _zip({"lambda_function.py": source})},
        Architectures=["x86_64"],
        Timeout=15,
    )
    yield name
    client.delete_function(FunctionName=name)


def test_create_lists_function(svc, function):
    """Control plane: the created function shows up with the x86_64 default."""
    client = svc.get_client()
    cfg = client.get_function_configuration(FunctionName=function)
    assert cfg["Runtime"] == RUNTIME
    # Real AWS Lambda defaults to x86_64; we pin it explicitly on create.
    assert cfg["Architectures"] == ["x86_64"]


def test_invoke_runs_handler_for_real(svc, function):
    """Execution: the handler actually runs and returns its output."""
    status, payload, _ = _invoke(svc, function, {"hello": "world"})
    assert status == 200
    assert payload == {"ok": True, "echo": {"hello": "world"}, "who": "oblako"}


def test_layer_zip_is_importable_at_runtime(svc):
    """A layer published as a zip is unpacked onto /opt and importable.

    The layer name deliberately does NOT end in 'layer': moto derives the /opt
    mount-volume name from the version ARN by splitting on 'layer:', so a name
    ending in 'layer' collapses the volume name to the version digit and Docker
    rejects it. The dashboard guards against that at publish time.
    """
    client = svc.get_client()
    layer = client.publish_layer_version(
        LayerName="oblako-testlib",
        Content={
            "ZipFile": _zip(
                {
                    "python/oblako_testlib/__init__.py": "def ping():\n    return 'pong'\n",
                }
            )
        },
        CompatibleRuntimes=[RUNTIME],
    )
    name = f"oblako-test-{uuid.uuid4().hex[:8]}"
    source = (
        "import oblako_testlib\n\n"
        "def handler(event, context):\n"
        "    return {'msg': oblako_testlib.ping()}\n"
    )
    client.create_function(
        FunctionName=name,
        Runtime=RUNTIME,
        Role=svc.ensure_exec_role(),
        Handler="lambda_function.handler",
        Code={"ZipFile": _zip({"lambda_function.py": source})},
        Architectures=["x86_64"],
        Layers=[layer["LayerVersionArn"]],
        Timeout=15,
    )
    try:
        status, payload, _ = _invoke(svc, name, {})
        assert status == 200
        assert payload == {"msg": "pong"}
    finally:
        client.delete_function(FunctionName=name)
