"""Base service class using docker-py."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

import docker
from docker.errors import NotFound, APIError, DockerException


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
    _client: docker.DockerClient | None = field(default=None, repr=False)

    @property
    def client(self) -> docker.DockerClient:
        """Return (or lazily create) the Docker client."""
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    @property
    def container_name(self) -> str:
        """Return the Docker container name for this service."""
        return f"oblako-ml-{self.name}"

    def _port_bindings(self) -> dict:
        return {f"{p.container_port}/{p.protocol}": p.host_port for p in self.ports}

    def _exposed_ports(self) -> list:
        return [f"{p.container_port}/{p.protocol}" for p in self.ports]

    # -----------------------------------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------------------------------
    def start(self) -> None:
        """Pull image if needed and start the container."""
        try:
            self.client.images.get(self.image)
        except NotFound:
            print(f"Pulling {self.image}...")
            self.client.images.pull(self.image)

        # Remove existing container if stopped
        try:
            existing = self.client.containers.get(self.container_name)
            if existing.status != "running":
                existing.remove(force=True)
            else:
                print(f"{self.name} is already running")
                return
        except NotFound:
            pass

        print(f"Starting {self.name}...")
        self.client.containers.run(
            self.image,
            name=self.container_name,
            detach=True,
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
        try:
            container = self.client.containers.get(self.container_name)
            container.stop(timeout=10)
            container.remove()
            print(f"{self.name} stopped")
        except NotFound:
            pass

    # -----------------------------------------------------------------------------------------------
    # Status and Health
    # -----------------------------------------------------------------------------------------------
    def status(self) -> ServiceStatus:
        """Get current service status."""
        try:
            container = self.client.containers.get(self.container_name)
            if container.status == "running":
                return ServiceStatus.RUNNING
            return ServiceStatus.STOPPED
        except NotFound:
            return ServiceStatus.STOPPED
        except (APIError, DockerException):
            return ServiceStatus.ERROR

    def logs(self, tail: int = 50) -> str:
        """Get recent container logs."""
        try:
            container = self.client.containers.get(self.container_name)
            return container.logs(tail=tail).decode("utf-8")
        except NotFound:
            return ""

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
