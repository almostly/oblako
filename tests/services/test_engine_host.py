"""`oblako up <engine>`: in-process engines as background services (no Docker)."""

import socket
import time

import boto3
import pytest

from oblako.engines import host


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """S3 Vectors (pure in-memory) on a free port, with state under tmp_path."""
    port = _free_port()
    monkeypatch.setattr(host, "STATE", tmp_path)
    monkeypatch.setitem(host.ENGINES, "s3vectors-test", ("s3vectors", port))
    yield "s3vectors-test", port
    host.stop("s3vectors-test")


def test_up_serves_plain_boto3_until_down(engine):
    name, port = engine
    assert host.status(name) == "stopped"
    assert host.start(name) == f"http://localhost:{port}"
    assert host.status(name) == "running"
    assert host.start(name) == f"http://localhost:{port}"  # idempotent

    # a client with no oblako code in it, as a reader would point boto3 at it
    client = boto3.client(
        "s3vectors",
        endpoint_url=f"http://localhost:{port}",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    client.create_vector_bucket(vectorBucketName="host-test")
    names = [
        b["vectorBucketName"] for b in client.list_vector_buckets()["vectorBuckets"]
    ]
    assert "host-test" in names
    # the engine answers a moment before it logs its banner, so allow for it
    for _ in range(50):
        if "serving on" in host.logfile(name).read_text():
            break
        time.sleep(0.1)
    assert "serving on" in host.logfile(name).read_text()

    assert host.stop(name) is True
    for _ in range(50):
        if host.status(name) == "stopped":
            break
        time.sleep(0.1)
    assert host.status(name) == "stopped"
    assert host.stop(name) is False  # nothing left to stop


def test_up_reports_a_port_another_process_holds(engine):
    name, port = engine
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", port))
        squatter.listen()
        with pytest.raises(RuntimeError, match=f"port {port} is in use"):
            host.start(name, timeout=15)
    assert not (host.STATE / "run" / f"{name}.pid").exists()
