"""The Service -> boto3 boundary.

Every oblako boto3 client points at a local endpoint with the same throwaway
creds and the active region. Centralizing that here means cross-cutting changes
— credentials, region handling, a default botocore Config, or one day a move off
moto — happen in one place instead of being copy-pasted across every service.

``client()`` is the low-level factory that every service funnels through, even
the bespoke ones. ``BotoService`` is a mixin for the common case: a Service that
exposes ``endpoint_url`` and speaks one or more AWS APIs there with the standard
creds + active region. Services that need autostart, a non-default region,
multiple endpoints, or a custom Config keep their own ``get_client`` and call
``client()`` directly — so the credential boundary stays single even for them.
"""

from __future__ import annotations

import boto3

from oblako import config


def client(service: str, endpoint_url: str, *, region: str | None = None, **overrides):
    """Return a boto3 client for ``service`` pointed at a local oblako endpoint.

    ``region`` defaults to the active ``config.region()``. Extra botocore kwargs
    (e.g. ``config=Config(...)`` for the S3 presigner) pass through via overrides.
    """
    return boto3.client(
        service,
        endpoint_url=endpoint_url,
        region_name=region or config.region(),
        aws_access_key_id="test",
        aws_secret_access_key="test",
        **overrides,
    )


def resource(
    service: str, endpoint_url: str, *, region: str | None = None, **overrides
):
    """Return a boto3 resource — the higher-level API, paralleling :func:`client`."""
    return boto3.resource(
        service,
        endpoint_url=endpoint_url,
        region_name=region or config.region(),
        aws_access_key_id="test",
        aws_secret_access_key="test",
        **overrides,
    )


class BotoService:
    """Mixin: ``get_client(service=aws_services[0], **overrides)``.

    For a service exposing ``endpoint_url`` that speaks the listed AWS API(s)
    with the standard local creds + active region::

        class GlueCatalogService(BotoService):
            aws_services = ("glue",)

        class IamService(BotoService):
            aws_services = ("iam", "sts")  # get_client() -> iam, ("sts") -> sts

    ``aws_services[0]`` is the default; pass another listed service to
    ``get_client()`` for multi-API services. A real method (not one a decorator
    attaches) so type checkers see it.
    """

    aws_services: tuple[str, ...] = ()

    @property
    def endpoint_url(self) -> str:
        """The local endpoint; every service using the mixin defines it."""
        raise NotImplementedError

    def get_client(self, service: str | None = None, **overrides):
        """Return a boto3 client for ``service`` at this service's endpoint."""
        return client(service or self.aws_services[0], self.endpoint_url, **overrides)
