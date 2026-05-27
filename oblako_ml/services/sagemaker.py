"""SageMaker local mode service: training, endpoints, and processing via Docker."""

from __future__ import annotations

import docker
from docker.errors import NotFound


class SageMakerService:
    """Wrapper around SageMaker SDK local mode.

    SageMaker local mode uses Docker directly (no separate container needed).
    This class provides helpers for managing training images, checking status,
    and cleaning up local mode artifacts.
    """

    def __init__(self):
        """Initialize SageMaker local mode with a deferred Docker client."""
        self._client: docker.DockerClient | None = None

    @property
    def client(self) -> docker.DockerClient:
        """Return (or lazily create) the Docker client."""
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def get_session(self):
        """Return a SageMaker LocalSession for local mode training/inference."""
        from sagemaker.local import LocalSession
        return LocalSession()

    def build_image(self, path: str, tag: str) -> str:
        """Build a training/inference Docker image."""
        image, logs = self.client.images.build(path=path, tag=tag)
        for chunk in logs:
            if "stream" in chunk:
                print(chunk["stream"], end="")
        return image.tags[0]

    def list_training_containers(self) -> list[dict]:
        """List running SageMaker local mode containers."""
        containers = self.client.containers.list(filters={"name": "sagemaker-local"})
        return [
            {"id": c.short_id, "name": c.name, "status": c.status, "image": c.image.tags}
            for c in containers
        ]

    def list_endpoint_containers(self) -> list[dict]:
        """List running SageMaker local endpoint containers."""
        containers = self.client.containers.list(filters={"name": "sagemaker-local"})
        return [
            {"id": c.short_id, "name": c.name, "status": c.status, "ports": c.ports}
            for c in containers
            if any("8080" in str(p) for p in c.ports.values())
        ]

    def cleanup(self) -> int:
        """Remove stopped SageMaker local mode containers."""
        removed = 0
        containers = self.client.containers.list(all=True, filters={"name": "sagemaker-local"})
        for c in containers:
            if c.status != "running":
                c.remove(force=True)
                removed += 1
        return removed

    def image_exists(self, tag: str) -> bool:
        """Check if a training/inference image exists locally."""
        try:
            self.client.images.get(tag)
            return True
        except NotFound:
            return False
