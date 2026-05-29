"""Local MLflow App — see :mod:`oblako.services.mlflow` for the managed container.

The Dockerfile next to this module is the image MlflowService builds on first
start; ``oblako.mlflow`` re-exports the service for ``from oblako.engines import mlflow``.
"""

from oblako.services.mlflow import MlflowService, IMAGE_TAG, ARTIFACT_BUCKET

__all__ = ["MlflowService", "IMAGE_TAG", "ARTIFACT_BUCKET"]
