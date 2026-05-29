"""Glue Data Catalog accessor: boto3 ``glue`` client over the Iceberg REST catalog.

The in-process server lives in :mod:`oblako.glue_catalog`. This class is the
thin Service-style accessor that starts it on demand and hands out a boto3
``glue`` client pointed at it.
"""

from __future__ import annotations

from . import boto


class GlueCatalogService:
    """Local Glue Data Catalog (boto3 ``glue``) bridged to the Iceberg REST catalog."""

    name = "glue_catalog"

    def __init__(self, port: int = 8486):
        """Initialize the accessor for the in-process Glue-API shim on the given port."""
        self.port = port

    @property
    def endpoint_url(self) -> str:
        """Endpoint URL boto3 clients connect to."""
        return f"http://localhost:{self.port}"

    def start_server(self) -> str:
        """Start the in-process Glue-API shim (idempotent)."""
        from oblako.glue_catalog import start_in_thread

        return start_in_thread(port=self.port)

    def get_client(self, autostart: bool = True):
        """Return a boto3 ``glue`` client (auto-starts the shim if it isn't running)."""
        from oblako import glue_catalog

        if autostart and not glue_catalog.is_running(self.port):
            self.start_server()
        return boto.client("glue", self.endpoint_url)
