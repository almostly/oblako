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

It also answers the Redshift-only catalog columns that reflection drivers read
(``pg_class.reldiststyle``, ``pg_attribute.attencodingtype`` …) with neutral
literals, since PostgreSQL's system catalogs can't grow those columns; see
``_rewrite_catalog``.

TLS: the proxy terminates SSL with a fixed self-signed cert baked into the image
(stable across ``down -v`` and clones) and forwards plaintext to PostgreSQL on the
loopback. So clients connect with ``sslmode=require`` (encrypt) exactly as they
would against real Redshift, no ``ssl=False`` local special-case. To fully verify,
point ``sslrootcert`` at the container's cert. Disable with OBLAKO_SSL=0.

Env:
  OBLAKO_PROXY_PORT  port to listen on            (default 5439, Redshift's port)
  OBLAKO_PG_HOST     upstream PostgreSQL host     (default 127.0.0.1)
  OBLAKO_PG_PORT     upstream PostgreSQL port     (default 5433)
  OBLAKO_SSL         offer TLS (1, default) or not (0)
  OBLAKO_SSL_CERT    server cert path             (default /etc/oblako-redshift/server.crt)
  OBLAKO_SSL_KEY     server key path              (default /etc/oblako-redshift/server.key)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import ssl
import struct

LISTEN_PORT = int(os.environ.get("OBLAKO_PROXY_PORT", "5439"))
PG_HOST = os.environ.get("OBLAKO_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("OBLAKO_PG_PORT", "5433"))

# Redshift version to present to the client in the startup ParameterStatus. Set
# only on the Citus MPP variant, where the engine can't spoof server_version
# itself (it breaks CREATE EXTENSION citus), so the proxy rewrites the value on
# the wire while the engine keeps its real version. Unset on the single-node image
# (the oblako_redshift extension spoofs it in-engine there), so server->client is
# a transparent byte copy.
PROXY_SERVER_VERSION = os.environ.get("OBLAKO_PROXY_SERVER_VERSION") or None

SSL_REQUEST = 80877103
GSSENC_REQUEST = 80877104


def _load_ssl_context() -> ssl.SSLContext | None:
    """A TLS server context from the cert/key, or None if SSL is off/absent."""
    if os.environ.get("OBLAKO_SSL", "1") != "1":
        return None
    cert = os.environ.get("OBLAKO_SSL_CERT", "/etc/oblako-redshift/server.crt")
    key = os.environ.get("OBLAKO_SSL_KEY", "/etc/oblako-redshift/server.key")
    if not (os.path.exists(cert) and os.path.exists(key)):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


SSL_CTX = _load_ssl_context()

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

# Redshift reflection drivers (sqlalchemy-redshift, and thus Alembic) run three
# fixed catalog queries against a forked pg_catalog. Each SELECTs local relations
# and UNIONs in Spectrum / late-binding-view externals, using Redshift-only SQL
# PostgreSQL rejects. oblako has no external catalog, so the proxy drops those
# UNION branches (always empty here) and translates what remains:
#
#   * Redshift-only catalog columns -> neutral literals, honest for a row-store
#     engine with no distribution / sort keys or encodings: pg_class.reldiststyle,
#     pg_attribute.attencodingtype and .attsortkeyord -> 0, .attisdistkey ->
#     false, and the pre-PG12 pg_attrdef.adsrc -> NULL.
#   * output-column aliases used in WHERE (a Redshift extension) -> the real
#     column: `schema` -> n.nspname, `table_name` -> c.relname (`relname` in the
#     relations query is already the real pg_class column). This keeps the
#     filter working, which is how has_table decides a table exists.
#
# Dropping the external branches first makes each alias unambiguous (one SELECT
# left). format_encoding() and the svv_external_* views the same drivers may
# query directly live in the engine (initdb.d/05_catalog_views.sql). Gated on a
# Redshift-specific marker so ordinary catalog queries pass through untouched.
_CATALOG_MARKER = re.compile(
    r"(?i)\b(?:reldiststyle|attencodingtype|attisdistkey|attsortkeyord"
    r"|svv_external_\w+|pg_get_late_binding_view_cols)\b"
)
_EXTERNAL_BRANCH = re.compile(r"(?i)svv_external_\w+|pg_get_late_binding_view_cols")
_UNION = re.compile(r"(?i)\bunion\b(?!\s+all\b)")
_CATALOG_REWRITES = [
    (re.compile(r"(?i)\b\w+\.reldiststyle\b"), "0"),
    (re.compile(r"(?i)\b\w+\.attencodingtype\b"), "0"),
    (re.compile(r"(?i)\b\w+\.attisdistkey\b"), "false"),
    (re.compile(r"(?i)\b\w+\.attsortkeyord\b"), "0"),
    # pre-PG12 pg_attrdef.adsrc, referenced unqualified (not <alias>.adsrc).
    (re.compile(r'(?i)(?<![."\w])adsrc\b'), "NULL::text AS adsrc"),
    # output-column aliases used in WHERE -> the real columns they alias.
    (re.compile(r"(?i)\band\s+schema\s*=\s*('[^']*')"), r"AND n.nspname = \1"),
    (re.compile(r"(?i)\band\s+table_name\s*=\s*('[^']*')"), r"AND c.relname = \1"),
]


def _rewrite_catalog(sql: str) -> str:
    """Make Redshift's reflection queries run on PostgreSQL (gated). See above."""
    if not _CATALOG_MARKER.search(sql):
        return sql
    # Drop the always-empty Spectrum / late-binding UNION branches. The first
    # (local-relations) branch never carries an external marker, so it survives.
    parts = _UNION.split(sql)
    if len(parts) > 1:
        kept = [p for p in parts if not _EXTERNAL_BRANCH.search(p)]
        if kept:
            sql = " UNION ".join(kept)
    for pat, repl in _CATALOG_REWRITES:
        sql = pat.sub(repl, sql)
    return sql


def rewrite_sql(sql: str) -> str:
    """Rewrite Redshift-only SQL PostgreSQL can't parse.

    ``VARCHAR(MAX)`` -> ``text`` (any statement); Redshift-only pg_catalog columns
    reflection drivers read are answered with neutral literals (see
    ``_rewrite_catalog``); Redshift physical-DDL storage clauses (DISTSTYLE/
    DISTKEY/SORTKEY/ENCODE/BACKUP) are stripped from CREATE TABLE. Everything else
    is left untouched.
    """
    s = _VARCHAR_MAX.sub("text", sql)
    s = _rewrite_catalog(s)
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


def _rewrite_parameter_status(body: bytes) -> bytes:
    """Rewrite a ParameterStatus ('S') body if it reports server_version."""
    i = body.index(b"\x00")
    if body[:i] != b"server_version":
        return b"S" + struct.pack("!I", len(body) + 4) + body
    new = b"server_version\x00" + PROXY_SERVER_VERSION.encode() + b"\x00"
    return b"S" + struct.pack("!I", len(new) + 4) + new


async def _pipe_server_startup(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Server -> client, rewriting the server_version ParameterStatus.

    Only the startup burst (auth + ParameterStatus + BackendKeyData up to the first
    ReadyForQuery 'Z') is framed and inspected, since server_version is sent there
    and never changes. After that we fall back to a raw byte copy, so query results
    take the fast path. Used only when PROXY_SERVER_VERSION is set (Citus variant).
    """
    try:
        while True:
            header = await reader.readexactly(5)  # type(1) + length(4)
            type_byte = header[:1]
            length = struct.unpack("!I", header[1:])[0]
            body = await reader.readexactly(length - 4)
            if type_byte == b"S":
                writer.write(_rewrite_parameter_status(body))
            else:
                writer.write(header + body)
            await writer.drain()
            if type_byte == b"Z":  # ReadyForQuery: startup done, rest is raw
                break
        await _pipe_raw(reader, writer)
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
        with contextlib.suppress(Exception):
            writer.close()


async def _negotiate_startup(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> bytes | None:
    """Answer the client's SSL/GSS negotiation, then return its StartupMessage.

    On ``SSLRequest`` the proxy terminates TLS itself (reply 'S' + start_tls with
    the self-signed cert), so client<->proxy is encrypted while proxy<->PG stays
    plaintext on the loopback. If SSL is off, reply 'N'. The returned bytes are
    the raw StartupMessage (or CancelRequest) to forward to PostgreSQL.
    """
    while True:
        header = await reader.readexactly(4)
        length = struct.unpack("!I", header)[0]
        body = await reader.readexactly(length - 4)
        if length == 8 and struct.unpack("!I", body)[0] in (SSL_REQUEST, GSSENC_REQUEST):
            is_ssl = struct.unpack("!I", body)[0] == SSL_REQUEST
            if is_ssl and SSL_CTX is not None:
                writer.write(b"S")
                await writer.drain()
                await writer.start_tls(SSL_CTX)  # client<->proxy now encrypted
            else:
                writer.write(b"N")  # no TLS (or GSS, which we don't offer)
                await writer.drain()
            continue  # the real StartupMessage follows
        return header + body


async def _pipe_typed(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Client -> server (post-startup): rewrite SQL in Q/P, relay the rest."""
    try:
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
    # Terminate SSL and read the StartupMessage before opening the backend, so no
    # relay task touches the client stream during the TLS handshake.
    try:
        startup = await _negotiate_startup(client_reader, client_writer)
    except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
        with contextlib.suppress(Exception):
            client_writer.close()
        return
    try:
        server_reader, server_writer = await asyncio.open_connection(PG_HOST, PG_PORT)
    except OSError:
        with contextlib.suppress(Exception):
            client_writer.close()
        return
    server_writer.write(startup)  # forward the StartupMessage plaintext to PG
    await server_writer.drain()
    # server -> client: rewrite server_version on the wire (Citus variant) or copy
    # raw (single-node, where the engine spoofs it and there's nothing to change).
    server_to_client = (
        _pipe_server_startup if PROXY_SERVER_VERSION else _pipe_raw
    )
    await asyncio.gather(
        _pipe_typed(client_reader, server_writer),
        server_to_client(server_reader, client_writer),
    )


async def main() -> None:
    server = await asyncio.start_server(_handle, "0.0.0.0", LISTEN_PORT)
    tls = "on" if SSL_CTX is not None else "off"
    print(
        f"oblako redshift proxy: :{LISTEN_PORT} -> {PG_HOST}:{PG_PORT} (tls {tls})",
        flush=True,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
