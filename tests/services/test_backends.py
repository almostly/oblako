"""Unit tests for the pluggable container backend selection."""

import pytest

from oblako.services import backends


def test_default_is_docker(monkeypatch):
    monkeypatch.delenv("OBLAKO_CONTAINER_BACKEND", raising=False)
    assert isinstance(backends.get_backend(), backends.DockerBackend)


def test_podman_and_colima_use_docker_backend(monkeypatch):
    # Podman and Colima speak the Docker API, so they share DockerBackend.
    for runtime in ("podman", "colima"):
        monkeypatch.setenv("OBLAKO_CONTAINER_BACKEND", runtime)
        assert isinstance(backends.get_backend(), backends.DockerBackend)


def test_kubernetes_backend_selected(monkeypatch):
    monkeypatch.setenv("OBLAKO_CONTAINER_BACKEND", "kubernetes")
    assert isinstance(backends.get_backend(), backends.KubernetesBackend)


def test_socket_selection_for_runtimes(monkeypatch, tmp_path):
    # DOCKER_HOST always wins (docker-py honours it) — no explicit socket.
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/x.sock")
    assert backends._socket_for("colima") is None

    # Otherwise the runtime's socket is auto-detected if it exists — this is what
    # lets the compute paths (via docker_client) target podman/colima, not just
    # the default Docker socket.
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    monkeypatch.setattr(backends, "_CANDIDATE_SOCKETS", {"colima": [str(sock)]})
    assert backends._socket_for("colima") == f"unix://{sock}"
    assert backends._socket_for("podman") is None  # no candidate -> ambient/default


def test_run_port_conflict_gives_friendly_error(monkeypatch):
    # A held host port should surface a clear PortInUseError, not a raw Docker 500.
    from docker.errors import APIError

    class _Containers:
        def run(self, *a, **k):
            raise APIError(
                "500 Server Error: Bind for 0.0.0.0:5439 failed: "
                "port is already allocated"
            )

    class _Client:
        containers = _Containers()

    b = backends.DockerBackend()
    monkeypatch.setattr(b, "_client", _Client())
    with pytest.raises(backends.PortInUseError) as exc:
        b.run(
            name="oblako-redshift",
            image="x",
            ports={"5432/tcp": 5439},
            environment={},
            volumes={},
            extra_hosts={},
            command=None,
            working_dir=None,
            user=None,
        )
    assert "5439" in str(exc.value) and "already in use" in str(exc.value)
    # and it names the override that moves oblako's service instead
    assert "OBLAKO_PORT_REDSHIFT_PG=<port>" in str(exc.value)


def test_build_k8s_manifests():
    # A dynamodb-like Service maps to a Deployment + Service.
    manifest = backends.build_k8s_manifests(
        name="oblako-dynamodb",
        image="amazon/dynamodb-local:latest",
        ports={"8000/tcp": 8001},
        environment={"FOO": "bar"},
        volumes={"oblako-dynamodb": {"bind": "/home/dynamodblocal/data", "mode": "rw"}},
        extra_hosts={},
        command="-jar DynamoDBLocal.jar -sharedDb -dbPath ./data",
        working_dir="/home/dynamodblocal",
        user="root",
        namespace="oblako",
    )
    items = {i["kind"]: i for i in manifest["items"]}
    container = items["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "amazon/dynamodb-local:latest"
    assert container["args"] == [
        "-jar",
        "DynamoDBLocal.jar",
        "-sharedDb",
        "-dbPath",
        "./data",
    ]
    assert container["ports"] == [{"containerPort": 8000}]
    assert {"name": "FOO", "value": "bar"} in container["env"]
    assert container["workingDir"] == "/home/dynamodblocal"
    assert container["securityContext"] == {"runAsUser": 0}
    assert container["volumeMounts"][0]["mountPath"] == "/home/dynamodblocal/data"
    svc = items["Service"]["spec"]
    assert svc["selector"] == {"app": "oblako-dynamodb"}
    assert svc["ports"] == [{"name": "p8000", "port": 8000, "targetPort": 8000}]


def test_build_k8s_manifests_skips_host_gateway():
    # docker's "host-gateway" has no k8s equivalent, so no hostAliases is emitted.
    manifest = backends.build_k8s_manifests(
        name="sfn",
        image="i",
        ports={},
        environment={},
        volumes={},
        extra_hosts={"host.docker.internal": "host-gateway"},
        command=None,
        working_dir=None,
        user=None,
        namespace="oblako",
    )
    pod_spec = manifest["items"][0]["spec"]["template"]["spec"]
    assert "hostAliases" not in pod_spec


def test_docker_host_overrides_socket_autodetect(monkeypatch):
    monkeypatch.setenv("OBLAKO_CONTAINER_BACKEND", "colima")
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/whatever.sock")
    backend = backends.get_backend()
    assert isinstance(backend, backends.DockerBackend)
    assert (
        backend._base_url is None
    )  # honours DOCKER_HOST via from_env, no explicit socket
