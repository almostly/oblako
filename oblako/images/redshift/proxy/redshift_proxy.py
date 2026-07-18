"""A thin PostgreSQL wire proxy that makes the engine tolerate Redshift SQL.

PostgreSQL's parser rejects Redshift-only syntax (DISTSTYLE/DISTKEY/SORTKEY/
ENCODE) at parse time, before any in-engine extension can intervene. So the only
place to fix it is *before* the bytes reach the parser: this proxy sits in front
of the engine, relays the startup/auth handshake transparently, and rewrites the
SQL inside simple-query ('Q') and Parse ('P') messages, stripping the Redshift
physical-DDL clauses that PostgreSQL ignores anyway. Everything else is passed
through byte-for-byte, so md5 auth, the extended protocol, COPY, etc. are
untouched.

Bundled inside the redshift-local image: it listens on the published port and
forwards to PostgreSQL on an internal port, so from the outside it's still just
"the redshift container".

Env:
  OBLAKO_PROXY_PORT  port to listen on            (default 5439, Redshift's port)
  OBLAKO_PG_HOST     upstream PostgreSQL host     (default 127.0.0.1)
  OBLAKO_PG_PORT     upstream PostgreSQL port     (default 5433)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import struct

LISTEN_PORT = int(os.environ.get("OBLAKO_PROXY_PORT", "5439"))
PG_HOST = os.environ.get("OBLAKO_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("OBLAKO_PG_PORT", "5433"))

SSL_REQUEST = 80877103
GSSENC_REQUEST = 80877104

# Redshift physical-DDL clauses PostgreSQL doesn't understand. Stripped only from
# CREATE TABLE statements; they're storage hints with no effect on a PG engine.
# Matches plain / TEMP / TEMPORARY / UNLOGGED / GLOBAL|LOCAL TEMP table creates.
_CREATE_TABLE = re.compile(
    r"(?i)\bcreate\s+(?:(?:global|local)\s+)?(?:temp(?:orary)?\s+|unlogged\s+)?table\b"
)
_STRIPPERS = [
    re.compile(r"(?i)\bdiststyle\s+\w+"),
    re.compile(r"(?i)\bdistkey\s*(?:\([^)]*\))?"),
    re.compile(r"(?i)\b(?:compound\s+|interleaved\s+)?sortkey\s*(?:auto\s*)?(?:\([^)]*\))?"),
    re.compile(r"(?i)\bencode\s+\w+"),
    re.compile(r"(?i)\bbackup\s+(?:yes|no)"),
]

# Redshift VARCHAR(MAX) / CHARACTER VARYING(MAX): PostgreSQL has no (MAX) length,
# so map it to TEXT. Applied to any statement (CREATE/ALTER TABLE, casts), since
# the token only appears in type declarations and never in valid PG. (dlt's
# redshift destination emits this DDL.)
_VARCHAR_MAX = re.compile(r"(?i)\b(?:character\s+varying|varchar)\s*\(\s*max\s*\)")


def rewrite_sql(sql: str) -> str:
    """Rewrite Redshift-only SQL PostgreSQL can't parse.

    ``VARCHAR(MAX)`` -> ``text`` (any statement); Redshift physical-DDL storage
    clauses (DISTSTYLE/DISTKEY/SORTKEY/ENCODE/BACKUP) are stripped from CREATE
    TABLE. Everything else is left untouched.
    """
    s = _VARCHAR_MAX.sub("text", sql)
    if not _CREATE_TABLE.search(s):
        return s
    for pat in _STRIPPERS:
        s = pat.sub(" ", s)
    # tidy the artifacts the removals leave behind (without touching literals)
    s = re.sub(r" {2,}", " ", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*\)", ")", s)
    s = re.sub(r"\(\s+", "(", s)
    return s


def _rewrite_query_message(body: bytes) -> bytes:
    """Rewrite the SQL in a simple-query ('Q') message body (SQL + NUL)."""
    sql = body[:-1].decode("utf-8", "surrogatepass")
    new = rewrite_sql(sql).encode("utf-8", "surrogatepass") + b"\x00"
    return b"Q" + struct.pack("!I", len(new) + 4) + new


def _rewrite_parse_message(body: bytes) -> bytes:
    """Rewrite the SQL in a Parse ('P') message: name NUL query NUL <param types>."""
    i = body.index(b"\x00")  # end of statement name
    j = body.index(b"\x00", i + 1)  # end of query string
    query = body[i + 1 : j].decode("utf-8", "surrogatepass")
    new_query = rewrite_sql(query).encode("utf-8", "surrogatepass")
    new_body = body[: i + 1] + new_query + body[j:]
    return b"P" + struct.pack("!I", len(new_body) + 4) + new_body


async def _pipe_raw(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Server -> client: pass everything through untouched."""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def _pipe_rewriting(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Client -> server: relay startup/auth, rewrite SQL in Q/P messages."""
    try:
        # Startup phase: length-prefixed, no type byte. Pass SSL/GSS requests
        # through (the engine answers 'N'); the StartupMessage ends this phase.
        while True:
            header = await reader.readexactly(4)
            length = struct.unpack("!I", header)[0]
            body = await reader.readexactly(length - 4)
            writer.write(header + body)
            await writer.drain()
            if not (length == 8 and struct.unpack("!I", body)[0] in (SSL_REQUEST, GSSENC_REQUEST)):
                break  # that was the StartupMessage (or CancelRequest)

        # Typed phase: 1-byte type + Int32 length + body.
        while True:
            type_byte = await reader.readexactly(1)
            length_b = await reader.readexactly(4)
            length = struct.unpack("!I", length_b)[0]
            body = await reader.readexactly(length - 4)
            if type_byte == b"Q":
                writer.write(_rewrite_query_message(body))
            elif type_byte == b"P":
                writer.write(_rewrite_parse_message(body))
            else:
                writer.write(type_byte + length_b + body)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()



async def _handle(client_reader, client_writer) -> None:
    try:
        server_reader, server_writer = await asyncio.open_connection(PG_HOST, PG_PORT)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(
        _pipe_rewriting(client_reader, server_writer),
        _pipe_raw(server_reader, client_writer),
    )


async def main() -> None:
    server = await asyncio.start_server(_handle, "0.0.0.0", LISTEN_PORT)
    print(f"oblako redshift proxy: :{LISTEN_PORT} -> {PG_HOST}:{PG_PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
