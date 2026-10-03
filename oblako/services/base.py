"""Base service class: container lifecycle over a pluggable ContainerBackend."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from .backends import ABSENT, RUNNING, ContainerBackend, DockerBackend, get_backend


class ServiceStatus(str, Enum):
    """Possible lifecycle states for a managed service container."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


@dataclass
class PortMapping:
    """Map a container port to a host port for a single protocol."""

    container_port: int
    host_port: int
    protocol: str = "tcp"


@dataclass
class Service:
    """Base class for all oblako services."""

    name: str
    image: str
    ports: list[PortMapping] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    volumes: dict[str, dict] = field(default_factory=dict)
    extra_hosts: dict[str, str] = field(default_factory=dict)
    command: str | list[str] | None = None
    working_dir: str | None = None
    container_user: str | None = None  # OS user inside the container (not a DB user)
    # For oblako-owned images: a Dockerfile context to build from if the image
    # can't be pulled (not published yet / offline). Pull is still preferred.
    build_context: str | None = None
    backend: ContainerBackend = field(default_factory=get_backend, repr=False)
    _client: object = field(default=None, repr=False)

    @property
    def client(self):
        """A docker-py client for Docker-native helpers (e.g. SageMaker local mode)."""
        if self._client is not None:
            return self._client
        if isinstance(self.backend, DockerBackend):
            return self.backend.client
        import docker

        return docker.from_env()

    @property
    def container_name(self) -> str:
        """Return the container name for this service."""
        return f"oblako-{self.name}"

    def _port_bindings(self) -> dict:
        return {f"{p.container_port}/{p.protocol}": p.host_port for p in self.ports}

    def _exposed_ports(self) -> list:
        return [f"{p.container_port}/{p.protocol}" for p in self.ports]

    # Lifecycle
    def start(self) -> None:
        """Pull the image if needed and start the container (idempotent)."""
        if self.build_context:
            # Prefer the published image; build from source if it isn't pullable.
            try:
                self.backend.ensure_image(self.image)
            except Exception:  # any pull failure -> local build
                self.backend.build_image(self.image, self.build_context)
        else:
            self.backend.ensure_image(self.image)
        status = self.backend.status(self.container_name)
        if status == RUNNING:
            print(f"{self.name} is already running")
            return
        if status != ABSENT:  # stopped leftover — clear it before recreating
            self.backend.remove(self.container_name)
        print(f"Starting {self.name}...")
        self.backend.run(
            name=self.container_name,
            image=self.image,
            ports=self._port_bindings(),
            environment=self.environment,
            volumes=self.volumes,
            extra_hosts=self.extra_hosts,
            command=self.command,
            working_dir=self.working_dir,
            user=self.container_user,
        )

    def stop(self) -> None:
        """Stop and remove the container."""
        if self.backend.status(self.container_name) != ABSENT:
            self.backend.stop(self.container_name)
            print(f"{self.name} stopped")

    # Status and health
    def status(self) -> ServiceStatus:
        """Get current service status."""
        try:
            return (
                ServiceStatus.RUNNING
                if self.backend.status(self.container_name) == RUNNING
                else ServiceStatus.STOPPED
            )
        except Exception:  # backend/daemon unreachable
            return ServiceStatus.ERROR

    def logs(self, tail: int = 50) -> str:
        """Get recent container logs."""
        return self.backend.logs(self.container_name, tail=tail)

    def wait_ready(self, timeout: float = 30.0, interval: float = 1.0) -> bool:
        """Wait until the service is healthy. Override _health_check for custom logic."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._health_check():
                return True
            time.sleep(interval)
        return False

    def _health_check(self) -> bool:
        """Override in subclasses for service-specific readiness checks."""
        return self.status() == ServiceStatus.RUNNING

    def restart(self) -> None:
        """Stop and restart the service container."""
        self.stop()
        self.start()

    def __repr__(self) -> str:
        """Return a concise string representation of the service."""
        return (
            f"{type(self).__name__}(name={self.name!r}, status={self.status().value})"
        )
