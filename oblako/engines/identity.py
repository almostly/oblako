"""Tell oblako's in-process engines apart from anything else on their port.

oblako's ports are static (see ``oblako.ports``), so another local tool can hold
one: OpenSearch Dashboards on 5601, a second S3Proxy on 9000. An engine that
treated "something answered on my port" as "my server is up" would then send
its API calls to that tool and fail with confusing parse errors.

Every engine server is wrapped with :func:`identify`, which stamps each response
with an ``x-oblako-engine`` header. :func:`is_engine` accepts a port only when
that header names the engine, and :func:`claim_port` refuses to start an engine
on a port some other process is already listening on.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

HEADER = "x-oblako-engine"


def identify(app, name: str):
    """Wrap an ASGI app so every HTTP response carries ``x-oblako-engine: name``."""
    tag = (HEADER.encode(), name.encode())

    async def tagged(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)

        async def send_tagged(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), tag]}
            await send(message)

        return await app(scope, receive, send_tagged)

    return tagged


def is_engine(port: int, name: str, timeout: float = 0.5) -> bool:
    """Return True if the server on ``port`` is oblako's ``name`` engine.

    Any response counts (a 404 or 405 still carries the header), so this doesn't
    depend on which routes an engine serves.
    """
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/", timeout=timeout
        ) as resp:
            return resp.headers.get(HEADER) == name
    except urllib.error.HTTPError as err:
        return err.headers.get(HEADER) == name
    except Exception:
        return False


def claim_port(port: int, name: str) -> None:
    """Raise if another process is listening on ``port`` (checked before binding)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            return
    raise RuntimeError(
        f"port {port} is in use by another process, so oblako's {name} engine "
        f"can't start there; stop that process (e.g. `lsof -nP -iTCP:{port} "
        "-sTCP:LISTEN`) and retry"
    )
