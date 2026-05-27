"""Unit tests for the pluggable container backend selection."""

from oblako.services import backends


def test_default_is_docker(monkeypatch):
    monkeypatch.delenv("OBLAKO_CONTAINER_BACKEND", raising=False)
    assert isinstance(backends.get_backend(), backends.DockerBackend)


def test_podman_and_colima_use_docker_backend(monkeypatch):
    # Podman and Colima speak the Docker API, so they share DockerBackend.
    for runtime in ("podman", "colima"):
        monkeypatch.setenv("OBLAKO_CONTAINER_BACKEND", runtime)
        assert isinstance(backends.get_backend(), backends.DockerBackend)


def test_kubernetes_backend_selected_and_not_implemented(monkeypatch):
    monkeypatch.setenv("OBLAKO_CONTAINER_BACKEND", "kubernetes")
    backend = backends.get_backend()
    assert isinstance(backend, backends.KubernetesBackend)
    import pytest

    with pytest.raises(NotImplementedError):
        backend.run(name="x", image="y", ports={}, environment={}, volumes={},
                    extra_hosts={}, command=None, working_dir=None, user=None)


def test_docker_host_overrides_socket_autodetect(monkeypatch):
    monkeypatch.setenv("OBLAKO_CONTAINER_BACKEND", "colima")
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/whatever.sock")
    backend = backends.get_backend()
    assert isinstance(backend, backends.DockerBackend)
    assert backend._base_url is None  # honours DOCKER_HOST via from_env, no explicit socket
