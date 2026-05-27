"""Pluggable container backend so oblako isn't hard-wired to Docker.

``Service`` (see base.py) drives containers through a ``ContainerBackend`` instead
of calling docker-py directly. Docker, Podman, and Colima all speak the Docker
Engine API, so they share ``DockerBackend`` — the only difference is which socket
it talks to (honoured via ``DOCKER_HOST`` or auto-detected per runtime).
Kubernetes is a genuinely different control plane, so it gets its own backend.

Select with ``OBLAKO_CONTAINER_BACKEND`` = docker (default) | podman | colima |
kubernetes. ``DOCKER_HOST`` always wins for the Docker-API runtimes.
"""

from __future__ import annotations

import os

# container status normalised across backends
RUNNING = "running"
STOPPED = "stopped"
ABSENT = "absent"

# Best-effort socket locations when DOCKER_HOST is unset (the runtime's `start`
# command usually creates these). DOCKER_HOST overrides all of them.
_CANDIDATE_SOCKETS = {
    "colima": ["~/.colima/default/docker.sock"],
    "podman": [
        "$XDG_RUNTIME_DIR/podman/podman.sock",
        "~/.local/share/containers/podman/machine/podman.sock",
        "/run/podman/podman.sock",
    ],
}


class ContainerBackend:
    """Minimal container lifecycle interface a Service needs."""

    name = "container"

    def ensure_image(self, image: str) -> None:
        """Pull the image if it is not present locally."""
        raise NotImplementedError

    def run(self, *, name, image, ports, environment, volumes, extra_hosts,
            command, working_dir, user) -> None:
        """Create and start a detached container with the given spec."""
        raise NotImplementedError

    def status(self, name: str) -> str:
        """Return RUNNING, STOPPED, or ABSENT for the named container."""
        raise NotImplementedError

    def remove(self, name: str) -> None:
        """Force-remove the container if present."""
        raise NotImplementedError

    def stop(self, name: str) -> None:
        """Stop and remove the container (no-op if absent)."""
        raise NotImplementedError

    def logs(self, name: str, tail: int = 50) -> str:
        """Return the container's recent logs."""
        raise NotImplementedError


class DockerBackend(ContainerBackend):
    """Docker Engine API backend — also serves Podman and Colima via their sockets."""

    name = "docker"

    def __init__(self, base_url: str | None = None):
        """Initialize against an explicit socket URL, else the ambient Docker context."""
        self._base_url = base_url
        self._client = None

    @property
    def client(self):
        """Lazily create the docker-py client, honouring DOCKER_HOST or the active context."""
        if self._client is None:
            import docker

            self._client = docker.DockerClient(base_url=self._base_url) if self._base_url \
                else docker.from_env()
        return self._client

    def ensure_image(self, image: str) -> None:
        """Pull the image if it is not already present."""
        from docker.errors import NotFound

        try:
            self.client.images.get(image)
        except NotFound:
            print(f"Pulling {image}...")
            self.client.images.pull(image)

    def run(self, *, name, image, ports, environment, volumes, extra_hosts,
            command, working_dir, user) -> None:
        """Create and start a detached container."""
        self.client.containers.run(
            image, name=name, detach=True, ports=ports, environment=environment,
            volumes=volumes, extra_hosts=extra_hosts, command=command,
            working_dir=working_dir, user=user,
        )

    def status(self, name: str) -> str:
        """Return RUNNING/STOPPED/ABSENT for the container."""
        from docker.errors import NotFound

        try:
            container = self.client.containers.get(name)
        except NotFound:
            return ABSENT
        return RUNNING if container.status == "running" else STOPPED

    def remove(self, name: str) -> None:
        """Force-remove the container if present."""
        from docker.errors import NotFound

        try:
            self.client.containers.get(name).remove(force=True)
        except NotFound:
            pass

    def stop(self, name: str) -> None:
        """Stop and remove the container (no-op if absent)."""
        from docker.errors import NotFound

        try:
            container = self.client.containers.get(name)
        except NotFound:
            return
        container.stop(timeout=10)
        container.remove()

    def logs(self, name: str, tail: int = 50) -> str:
        """Return recent logs, or empty string if the container is gone."""
        from docker.errors import NotFound

        try:
            return self.client.containers.get(name).logs(tail=tail).decode("utf-8")
        except NotFound:
            return ""


class KubernetesBackend(ContainerBackend):
    """Run oblako services as Kubernetes workloads (e.g. on minikube).

    Planned: map each Service to a Deployment + Service, expose ports via
    NodePort or ``kubectl port-forward``, and read status from the pod phase.
    Not yet implemented — the abstraction exists so this is a drop-in addition.
    """

    name = "kubernetes"

    _MSG = ("The Kubernetes backend is scaffolded but not implemented yet. "
            "Use OBLAKO_CONTAINER_BACKEND=docker|podman|colima for now.")

    def ensure_image(self, image: str) -> None:
        """Not implemented."""
        raise NotImplementedError(self._MSG)

    def run(self, **kwargs) -> None:
        """Not implemented."""
        raise NotImplementedError(self._MSG)

    def status(self, name: str) -> str:
        """Not implemented."""
        raise NotImplementedError(self._MSG)

    def remove(self, name: str) -> None:
        """Not implemented."""
        raise NotImplementedError(self._MSG)

    def stop(self, name: str) -> None:
        """Not implemented."""
        raise NotImplementedError(self._MSG)

    def logs(self, name: str, tail: int = 50) -> str:
        """Not implemented."""
        raise NotImplementedError(self._MSG)


def _docker_backend_for(runtime: str) -> DockerBackend:
    if os.environ.get("DOCKER_HOST"):
        return DockerBackend()  # explicit DOCKER_HOST wins
    for candidate in _CANDIDATE_SOCKETS.get(runtime, []):
        path = os.path.expanduser(os.path.expandvars(candidate))
        if os.path.exists(path):
            return DockerBackend(base_url=f"unix://{path}")
    return DockerBackend()  # fall back to the ambient Docker context


def get_backend() -> ContainerBackend:
    """Return the configured container backend (OBLAKO_CONTAINER_BACKEND, default docker)."""
    choice = (os.environ.get("OBLAKO_CONTAINER_BACKEND") or "docker").lower()
    if choice in ("kubernetes", "k8s"):
        return KubernetesBackend()
    if choice in ("podman", "colima"):
        return _docker_backend_for(choice)
    return DockerBackend()
