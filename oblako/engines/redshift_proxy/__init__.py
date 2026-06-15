"""Postgres-wire shim that lets redshift-connector reach pgredshift, on Redshift's port.

Amazon's ``redshift-connector`` driver (used by dbt-redshift, and anything else
that speaks to Redshift from Python) puts Redshift-only parameters in the
Postgres StartupMessage — ``client_protocol_version``, ``driver_version``,
``os_version``, ``driver_discovery_version``. Real Redshift registers these as
GUCs; pgredshift (Postgres 10) does not, so it rejects the connection at the
handshake with ``FATAL: unrecognized configuration parameter
"client_protocol_version"``. There is no client-side switch to suppress them,
so without this shim a redshift-connector client cannot connect at all.

This proxy *is* the Redshift endpoint (port 5439). It forwards to the raw
pgredshift engine (port 5438) and rewrites only what the handshake needs:

  * StartupMessage — drops everything outside a known-safe allowlist, so the
    Redshift-only parameters never reach pgredshift.
  * server_version — pgredshift reports its Postgres version ("10.18 (Debian …)")
    which redshift-connector can't parse; real Redshift reports "8.0.2", so we
    substitute that. Applied *only* to connections that announced themselves as
    redshift-connector, so native psycopg2 clients pass through untouched and
    still see the true engine version.

Everything after the handshake (auth, queries, result sets) is byte-tunnelled.

It is a plain-stdlib module with no oblako imports, so it also runs standalone
inside a python:slim container — see ``__main__`` and docker-compose.yml.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time

__all__ = ["serve", "start_in_thread", "is_running", "DEFAULT_LISTEN_PORT"]

DEFAULT_LISTEN_PORT = 5439  # Redshift's port — this proxy is the endpoint
DEFAULT_BACKEND_HOST = "localhost"
DEFAULT_BACKEND_PORT = 5438  # raw pgredshift engine, behind the proxy

# Postgres protocol request codes (the int that follows the length prefix).
_PROTOCOL_3_0 = 196608  # 0x00030000 — a real StartupMessage
_SSL_REQUEST = 80877103
_GSSENC_REQUEST = 80877104

# Real Redshift reports PostgreSQL 8.0.2; substituting it both parses cleanly and
# steers redshift-connector onto its genuine Redshift code path.
_SERVER_VERSION = b"8.0.2"

# The Redshift-only startup parameters redshift-connector sends. Their presence
# is how we recognize a redshift-connector client (vs. a plain psycopg2 client).
_REDSHIFT_ONLY_KEYS = frozenset(
    {
        b"client_protocol_version",
        b"driver_version",
        b"os_version",
        b"driver_discovery_version",
    }
)

# Startup parameters plain Postgres accepts. Anything else (notably the keys
# above) is dropped before forwarding. Allowlisting rather than denylisting
# future-proofs against the driver adding new Redshift-only keys: the only
# parameters a connection *needs* are user and database; the rest are GUCs with
# server-side defaults.
_ALLOWED_STARTUP_KEYS = frozenset(
    {
        b"user",
        b"database",
        b"options",
        b"replication",
        b"application_name",
        b"client_encoding",
        b"search_path",
        b"DateStyle",
        b"IntervalStyle",
        b"TimeZone",
        b"extra_float_digits",
        b"standard_conforming_strings",
    }
)


# ---------------------------------------------------------------------------
# StartupMessage rewriting
# ---------------------------------------------------------------------------
def _rewrite_startup(body: bytes) -> tuple[bytes, bool]:
    r"""Rebuild a StartupMessage, keeping only allowlisted parameters.

    ``body`` is the message minus its 4-byte length prefix: a 4-byte protocol
    version followed by null-terminated ``key\0value\0…\0`` pairs and a final
    terminating null. Returns ``(message, is_redshift_connector)`` — the complete
    rebuilt message (length prefix included), and whether the client sent any
    Redshift-only parameter.
    """
    version, params = body[:4], body[4:]
    tokens = params.split(b"\x00")

    kept = bytearray(version)
    is_redshift_connector = False
    i = 0
    while i + 1 < len(tokens):
        key = tokens[i]
        if key == b"":  # reached the terminator
            break
        value = tokens[i + 1]
        if key in _REDSHIFT_ONLY_KEYS:
            is_redshift_connector = True
        if key in _ALLOWED_STARTUP_KEYS:
            kept += key + b"\x00" + value + b"\x00"
        i += 2
    kept += b"\x00"  # final terminator

    message = (len(kept) + 4).to_bytes(4, "big") + bytes(kept)
    return message, is_redshift_connector


def _rewrite_server_version(body: bytes) -> bytes | None:
    """Return a ParameterStatus body with server_version overridden, else None."""
    name, sep, _rest = body.partition(b"\x00")
    if not sep or name != b"server_version":
        return None
    return name + b"\x00" + _SERVER_VERSION + b"\x00"


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------
async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes one way until EOF, then half-close the destination."""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _pipe_backend(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Backend -> client copy that rewrites the server_version ParameterStatus.

    Frames messages (``[type][int32 len][body]``) only until ReadyForQuery ('Z')
    arrives — the version banner is sent once, before the connection is usable —
    then switches to raw passthrough so result streaming stays zero-copy.
    """
    buf = bytearray()
    raw = False
    try:
        while data := await reader.read(65536):
            if raw:
                writer.write(data)
                await writer.drain()
                continue
            buf += data
            out = bytearray()
            while len(buf) >= 5:
                mtype = buf[0:1]
                mlen = int.from_bytes(buf[1:5], "big")
                if len(buf) < 1 + mlen:
                    break  # message not fully arrived yet
                body = bytes(buf[5 : 1 + mlen])
                del buf[: 1 + mlen]

                rewritten = _rewrite_server_version(body) if mtype == b"S" else None
                if rewritten is not None:
                    out += b"S" + (len(rewritten) + 4).to_bytes(4, "big") + rewritten
                else:
                    out += mtype + mlen.to_bytes(4, "big") + body

                if mtype == b"Z":  # ReadyForQuery — banner done, go raw
                    raw = True
                    out += buf
                    buf.clear()
                    break
            if out:
                writer.write(out)
                await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    backend_host: str,
    backend_port: int,
) -> None:
    """Negotiate the handshake, then tunnel the connection to the backend."""
    backend_writer: asyncio.StreamWriter | None = None
    try:
        # Consume any SSL/GSS negotiation up front. pgredshift is plaintext, so
        # we answer "not supported" ourselves; sslmode: disable / prefer then
        # proceed over plaintext. The real StartupMessage follows.
        while True:
            header = await client_reader.readexactly(4)
            length = int.from_bytes(header, "big")
            body = await client_reader.readexactly(length - 4)
            code = int.from_bytes(body[:4], "big")

            if code in (_SSL_REQUEST, _GSSENC_REQUEST):
                client_writer.write(b"N")
                await client_writer.drain()
                continue

            backend_reader, backend_writer = await asyncio.open_connection(
                backend_host, backend_port
            )
            if code == _PROTOCOL_3_0:
                startup, is_redshift_connector = _rewrite_startup(body)
                backend_writer.write(startup)
            else:
                # CancelRequest or anything else carries no startup params.
                backend_writer.write(header + body)
                is_redshift_connector = False
            await backend_writer.drain()
            break

        # Only redshift-connector needs the version banner massaged; everyone
        # else (psycopg2) gets the real engine, byte-for-byte.
        backend_to_client = (
            _pipe_backend(backend_reader, client_writer)
            if is_redshift_connector
            else _pipe(backend_reader, client_writer)
        )
        await asyncio.gather(
            _pipe(client_reader, backend_writer),
            backend_to_client,
        )
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        client_writer.close()
        if backend_writer is not None:
            backend_writer.close()


async def _serve(listen_port: int, backend_host: str, backend_port: int) -> None:
    """Run the proxy server until cancelled."""

    async def handler(r, w):
        await _handle_client(r, w, backend_host, backend_port)

    server = await asyncio.start_server(handler, "0.0.0.0", listen_port)
    async with server:
        await server.serve_forever()


def serve(
    listen_port: int = DEFAULT_LISTEN_PORT,
    backend_host: str = DEFAULT_BACKEND_HOST,
    backend_port: int = DEFAULT_BACKEND_PORT,
) -> None:
    """Run the proxy in the foreground (blocking) until interrupted."""
    try:
        asyncio.run(_serve(listen_port, backend_host, backend_port))
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Background / lifecycle helpers
# ---------------------------------------------------------------------------
_threads: dict[int, threading.Thread] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_LISTEN_PORT, timeout: float = 0.5) -> bool:
    """Return True if something is accepting connections on the port."""
    try:
        with socket.create_connection(("localhost", port), timeout=timeout):
            return True
    except OSError:
        return False


def start_in_thread(
    listen_port: int = DEFAULT_LISTEN_PORT,
    backend_host: str = DEFAULT_BACKEND_HOST,
    backend_port: int = DEFAULT_BACKEND_PORT,
) -> int:
    """Start the proxy in a daemon thread (idempotent). Returns the listen port."""
    if is_running(listen_port):
        return listen_port
    with _lock:
        if listen_port in _threads:
            return listen_port

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_serve(listen_port, backend_host, backend_port))

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        _threads[listen_port] = thread

    deadline = time.time() + 10
    while time.time() < deadline:
        if is_running(listen_port):
            return listen_port
        time.sleep(0.1)
    raise RuntimeError(f"redshift proxy did not start on port {listen_port}")


def _main() -> None:
    """Standalone entrypoint (used by the container). Reads ports from env."""
    serve(
        listen_port=int(os.environ.get("OBLAKO_RS_PROXY_LISTEN", DEFAULT_LISTEN_PORT)),
        backend_host=os.environ.get(
            "OBLAKO_RS_PROXY_BACKEND_HOST", DEFAULT_BACKEND_HOST
        ),
        backend_port=int(
            os.environ.get("OBLAKO_RS_PROXY_BACKEND_PORT", DEFAULT_BACKEND_PORT)
        ),
    )


if __name__ == "__main__":
    _main()
