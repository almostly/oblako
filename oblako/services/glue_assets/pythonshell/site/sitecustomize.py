"""Send boto3, awswrangler and s3fs to oblako's endpoints, and trust its Redshift.

botocore reads ``AWS_ENDPOINT_URL_<SERVICE>`` itself only from 1.31 on, and Glue's
Python shell 3.9 ships 1.24. This fills a client's ``endpoint_url`` from those
variables when the caller gives none, so job scripts carry no endpoint. The
runner also mounts the keeper ``oblako trust`` writes, which adds oblako's
Redshift certificates to redshift_connector's CA bundle.
"""

from __future__ import annotations

import functools
import inspect
import os
from collections.abc import Callable
from typing import Any


@functools.lru_cache(maxsize=None)
def _endpoint_key(service_name: str) -> str:
    """Return a service's ``AWS_ENDPOINT_URL_`` suffix: its service id, upper snake case."""
    from botocore.session import get_session

    service_id = get_session().get_service_model(service_name).service_id
    return str(service_id).upper().replace(" ", "_").replace("-", "_")


def _endpoint(service_name: str) -> str | None:
    """Return the endpoint the environment names for a service, as newer botocore does."""
    key = _endpoint_key(service_name)
    return os.environ.get(f"AWS_ENDPOINT_URL_{key}") or os.environ.get(
        "AWS_ENDPOINT_URL"
    )


def _with_endpoint(create_client: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a session's client factory so a missing ``endpoint_url`` comes from the environment."""
    signature = inspect.signature(create_client)

    @functools.wraps(create_client)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        """Create the client, with the environment's endpoint when none is given."""
        bound = signature.bind(*args, **kwargs)
        if bound.arguments.get("endpoint_url") is None:
            url = _endpoint(bound.arguments["service_name"])
            if url:
                bound.arguments["endpoint_url"] = url
        return create_client(*bound.args, **bound.kwargs)

    return wrapper


try:
    from botocore.session import Session
except ImportError:
    pass
else:
    Session.create_client = _with_endpoint(Session.create_client)  # type: ignore[method-assign]

try:
    from aiobotocore.session import AioSession  # s3fs
except ImportError:
    pass
else:
    AioSession._create_client = _with_endpoint(AioSession._create_client)  # type: ignore[attr-defined]

try:
    import _oblako_redshift_trust  # noqa: F401
except ImportError:
    pass
