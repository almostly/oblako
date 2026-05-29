"""The Service -> boto3 boundary.

Every oblako boto3 client points at a local endpoint with the same throwaway
creds and the active region. Centralizing that here means cross-cutting changes
— credentials, region handling, a default botocore Config, or one day a move off
moto — happen in one place instead of being copy-pasted across every service.

``client()`` is the low-level factory that every service funnels through, even
the bespoke ones. ``@BotoService`` is sugar for the common case: a Service that
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


def resource(service: str, endpoint_url: str, *, region: str | None = None, **overrides):
    """Like :func:`client`, but a boto3 resource (the higher-level API)."""
    return boto3.resource(
        service,
        endpoint_url=endpoint_url,
        region_name=region or config.region(),
        aws_access_key_id="test",
        aws_secret_access_key="test",
        **overrides,
    )


class BotoService:
    """Class decorator: attach ``get_client(service=services[0], **overrides)``.

    Applied to a Service exposing ``endpoint_url`` that speaks the given AWS
    API(s) with the standard local creds + active region::

        @BotoService("glue")
        class GlueCatalogService(Service): ...

        @BotoService("iam", "sts")   # get_client() -> iam, get_client("sts") -> sts
        class IamService: ...

    ``services[0]`` is the default; pass another listed service to
    ``get_client()`` for multi-API services. It's a class (not a function) so it
    reads alongside the ``*Service`` classes it decorates — but you apply it with
    ``@``, you never instantiate a service from it.
    """

    def __init__(self, *services: str):
        """Record the AWS service name(s) this Service speaks (first is default)."""
        if not services:
            raise ValueError("BotoService needs at least one AWS service name")
        self.services = services

    def __call__(self, cls):
        """Attach a ``get_client`` bound to the decorated class's ``endpoint_url``."""
        default = self.services[0]

        def get_client(self, service: str = default, **overrides):
            return client(service, self.endpoint_url, **overrides)

        get_client.__doc__ = (
            f"boto3 {default!r} client pointed at this service's endpoint."
        )
        cls.get_client = get_client
        cls._aws_services = self.services
        return cls
