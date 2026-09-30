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

On the Citus MPP variant (OBLAKO_CITUS=1) it also turns Redshift distribution DDL
into real Citus distribution: it records what a ``CREATE TABLE`` with DISTKEY /
DISTSTYLE ALL should become and, once that transaction commits, runs
create_distributed_table / create_reference_table on a side connection (see
``_distribute``). There it also presents Redshift's server_version on the wire.

Env:
  OBLAKO_PROXY_PORT  port to listen on            (default 5439, Redshift's port)
  OBLAKO_PG_HOST     upstream PostgreSQL host     (default 127.0.0.1)
  OBLAKO_PG_PORT     upstream PostgreSQL port     (default 5433)
  OBLAKO_SSL         offer TLS (1, default) or not (0)
  OBLAKO_SSL_CERT    server cert path             (default /etc/oblako-redshift/server.crt)
  OBLAKO_SSL_KEY     server key path              (default /etc/oblako-redshift/server.key)
  OBLAKO_CITUS       (=1) auto-distribute DISTKEY/DISTSTYLE tables via Citus
  OBLAKO_PROXY_SERVER_VERSION  server_version to present to clients (Citus variant)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import ssl
import struct

# The COPY/UNLOAD <-> S3 bridge. Optional: if its deps (pydantic/boto3/pyarrow)
# aren't present the proxy still runs, just without COPY/UNLOAD rewriting.
try:
    import copy_unload
except Exception:  # noqa: BLE001 - any import failure disables the feature
    copy_unload = None

# SUPER (PartiQL) dot-navigation rewriting. Pure-stdlib; optional all the same.
try:
    import super_nav
except Exception:  # noqa: BLE001 - any import failure disables the feature
    super_nav = None

# LISTAGG -> string_agg rewriting. Pure-stdlib; optional all the same.
try:
    import listagg
except Exception:  # noqa: BLE001 - any import failure disables the feature
    listagg = None

# Bare datepart keywords (DATEADD(month, ...)) -> quoted. Pure-stdlib; optional.
try:
    import datepart
except Exception:  # any import failure disables the feature
    datepart = None

# CREATE/SHOW/DROP MODEL -> Redshift ML functions. Pure-stdlib; optional.
try:
    import redshift_ml
except Exception:  # any import failure disables the feature
    redshift_ml = None

# PIVOT/UNPIVOT -> standard SQL (needs sqlglot as a parser). Optional.
try:
    import pivot_unpivot
except Exception:  # noqa: BLE001 - any import failure disables the feature
    pivot_unpivot = None

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

# Citus MPP variant: when set, the proxy turns Redshift distribution DDL into real
# Citus distribution. It strips DISTKEY/DISTSTYLE as usual, but also records what
# the table should become and, once the CREATE's transaction commits, runs
# create_distributed_table / create_reference_table on a *side* connection (a
# separate transaction, since doing it inside the CREATE deadlocks). Unset on the
# single-node image, where distribution DDL is simply stripped.
OBLAKO_CITUS = os.environ.get("OBLAKO_CITUS") == "1"
PG_USER = os.environ.get("POSTGRES_USER", "oblako")
PG_DATABASE = os.environ.get("POSTGRES_DB", "oblako")

SSL_REQUEST = 80877103
GSSENC_REQUEST = 80877104
STARTUP_PROTOCOL = 196608  # 3.0


def _load_ssl_context() -> ssl.SSLContext | None:
    """Build a TLS server context from the cert/key, or None if SSL is off/absent."""
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
    re.compile(
        r"(?i)\b(?:compound\s+|interleaved\s+)?sortkey\s*(?:auto\s*)?(?:\([^)]*\))?"
    ),
    re.compile(r"(?i)\bencode\s+\w+"),
    re.compile(r"(?i)\bbackup\s+(?:yes|no)"),
]

# Redshift VARCHAR(MAX) / CHARACTER VARYING(MAX): PostgreSQL has no (MAX) length,
# so map it to TEXT. Applied to any statement (CREATE/ALTER TABLE, casts), since
# the token only appears in type declarations and never in valid PG. (dlt's
# redshift destination emits this DDL.)
_VARCHAR_MAX = re.compile(r"(?i)\b(?:character\s+varying|varchar)\s*\(\s*max\s*\)")

# Redshift CREATE USER ... CREATEUSER (a superuser-ish privilege) -> PostgreSQL
# SUPERUSER. Matches the one-word keyword, not the two-word "CREATE USER". redtape
# emits this for a superuser in its access-management specs.
_CREATEUSER = re.compile(r"(?i)\bcreateuser\b")

# Redshift CREATE/ALTER USER ... PASSWORD DISABLE -> PostgreSQL PASSWORD NULL.
# DISABLE is how an IAM-only Redshift account is provisioned: the account exists and
# holds grants, but carries no password. PostgreSQL spells that PASSWORD NULL, which
# leaves pg_shadow.passwd NULL exactly as Redshift does. PostgreSQL has no DISABLE
# keyword and rejects the statement outright, so any script provisioning passwordless
# accounts fails on the first line.
#
# This is about accepting the statement and recording the same account state, not
# about enforcement: the image ships pg_hba as trust and the proxy reaches the engine
# over loopback, so no password is checked for any account either way.
_PASSWORD_DISABLE = re.compile(r"(?i)\bpassword\s+disable\b")

# Hide PostgreSQL's predefined pg_* roles from pg_catalog.pg_group, so Redshift
# access tools (redtape) see only real groups (Redshift has no pg_* roles). Wrap
# the table in a filtered subquery: re.sub does not re-scan its replacement, so the
# inner pg_catalog.pg_group is not itself rewritten (no recursion, no stored view
# that the catalog tests would re-create through the proxy into a self-reference).
_PG_GROUP = re.compile(r"(?i)\bpg_catalog\.pg_group\b")
_PG_GROUP_SUB = (
    "(SELECT groname, grosysid, grolist FROM pg_catalog.pg_group "
    "WHERE groname !~ '^pg_') AS pg_group"
)

# ACL strings: Redshift prefixes a group grantee (`group analysts=r/bi_analyst`),
# PostgreSQL does not (`analysts=r/bi_analyst`), because roles and groups are
# unified. Clients parse that string, so without the prefix a group grant reads as a
# user grant: redtape then files the group as a user, sees it holding nothing, and
# re-plans the same GRANTs forever. Point any array_to_string over an ACL array at
# redshift_acl(), which takes the same two arguments and adds the prefix (see
# initdb.d/05_catalog_views.sql). Only the function name is replaced, so the
# arguments and any surrounding cast are left as written.
#
# An explicit pg_catalog. qualifier is consumed with it (sqlalchemy-redshift's
# reflection writes pg_catalog.array_to_string(c.relacl, ...)): redshift_acl lives in
# public, so leaving the qualifier would point at a schema it is not in. Every ACL
# reader gets the Redshift rendering, which is what the real cluster returns them.
#
# Ungated: an array_to_string over an *acl column is specific enough on its own, and
# redtape's tables query carries none of the catalog markers below.
_ACL_TO_STRING = re.compile(
    r"(?i)\b(?:pg_catalog\s*\.\s*)?array_to_string\s*\(\s*"
    r'(?=[\w".]*\b(?:rel|nsp|dat)acl\b)'
)

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
    r"|usecatupd|svv_external_\w+|pg_get_late_binding_view_cols)\b"
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
    # pg_user.usecatupd, dropped from PG >= 9.5; redtape's user introspection
    # selects it bare from pg_catalog.pg_user. Answer as a neutral literal.
    (re.compile(r'(?i)(?<![."\w])usecatupd\b'), "false AS usecatupd"),
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

    ``VARCHAR(MAX)`` -> ``text`` and ``PASSWORD DISABLE`` -> ``PASSWORD NULL``
    (any statement); Redshift ML's CREATE/SHOW/DROP MODEL become calls to the
    in-engine Redshift ML functions (see ``redshift_ml``); bare datepart keywords (``DATEADD(month, ...)``) are quoted
    (see ``datepart``); Redshift-only pg_catalog columns
    reflection drivers read are answered with neutral literals (see
    ``_rewrite_catalog``); ACL arrays are rendered with Redshift's ``group ``
    prefix (see ``_ACL_TO_STRING``); Redshift physical-DDL storage clauses (DISTSTYLE/
    DISTKEY/SORTKEY/ENCODE/BACKUP) are stripped from CREATE TABLE; ``COPY``/
    ``UNLOAD`` to/from ``s3://`` are rewritten into oblako_* S3 bridge calls (see
    ``copy_unload``). Everything else is left untouched.
    """
    s = sql
    if pivot_unpivot is not None:
        s = pivot_unpivot.rewrite_pivot_unpivot(s)  # PIVOT/UNPIVOT -> standard SQL
    if copy_unload is not None and copy_unload.has_s3_copy_or_unload(s):
        s = copy_unload.rewrite_copy_unload(s)
    if super_nav is not None:
        super_nav.record_super_columns(s)  # learn SUPER columns from DDL
        s = super_nav.rewrite_super_paths(s)  # dot-navigation -> jsonb path
    if listagg is not None:
        s = listagg.rewrite_listagg(s)  # LISTAGG -> string_agg
    if datepart is not None:
        s = datepart.rewrite_dateparts(s)  # DATEADD(month, ..) -> ('month', ..)
    s = _VARCHAR_MAX.sub("text", s)
    s = _CREATEUSER.sub("SUPERUSER", s)
    s = _PASSWORD_DISABLE.sub("PASSWORD NULL", s)
    s = _PG_GROUP.sub(_PG_GROUP_SUB, s)
    s = _ACL_TO_STRING.sub("redshift_acl(", s)
    s = _rewrite_catalog(s)
    if redshift_ml is not None:
        s = redshift_ml.rewrite_ml(s)  # CREATE/SHOW/DROP MODEL -> oblako_ml_* calls
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


# Capture the table name and (if present) the Redshift distribution/sort intent
# from a CREATE TABLE, so the Citus variant can turn it into real distribution.
# Names/columns are limited to identifier characters, so the values are safe to
# interpolate (no quotes/semicolons can appear). DISTKEY -> distributed, DISTSTYLE
# ALL -> reference; EVEN/AUTO/none stay local. SORTKEY -> a btree index on those
# columns (a stand-in for Redshift's sort order), but only on a distributed /
# reference table -- a plain local table is left untouched.
_CREATE_TABLE_NAME = re.compile(
    r"(?i)\bcreate\s+(?:(?:global|local)\s+)?(?:temp(?:orary)?\s+|unlogged\s+)?"
    r'table\s+(?:if\s+not\s+exists\s+)?"?([\w.$]+)"?'
)
_DISTKEY_COL = re.compile(r'(?i)\bdistkey\s*\(\s*"?([\w$]+)"?\s*\)')
_DISTSTYLE_ALL = re.compile(r"(?i)\bdiststyle\s+all\b")
_SORTKEY_COLS = re.compile(
    r"(?i)\b(?:compound\s+|interleaved\s+)?sortkey\s*\(\s*([\w$, ]+?)\s*\)"
)


def extract_distribution(sql: str) -> list[str]:
    """Return the Citus commands a CREATE TABLE's Redshift DDL implies (may be empty).

    ``DISTKEY(col)`` -> ``create_distributed_table``; ``DISTSTYLE ALL`` ->
    ``create_reference_table``; a ``SORTKEY`` on a distributed/reference table adds
    a btree index on those columns. Anything else (EVEN/AUTO/no distkey) stays a
    plain local table and yields no commands. Returned in the order they must run
    on the side connection after the CREATE commits.
    """
    if not _CREATE_TABLE.search(sql):
        return []
    name = _CREATE_TABLE_NAME.search(sql)
    if not name:
        return []
    table = name.group(1)
    commands: list[str] = []
    distkey = _DISTKEY_COL.search(sql)
    if distkey:
        commands.append(
            f"SELECT create_distributed_table('{table}', '{distkey.group(1)}')"
        )
    elif _DISTSTYLE_ALL.search(sql):
        commands.append(f"SELECT create_reference_table('{table}')")
    if commands:  # only index the sort key on a table we actually distribute
        sortkey = _SORTKEY_COLS.search(sql)
        if sortkey:
            cols = " ".join(sortkey.group(1).split())  # normalize whitespace
            commands.append(f"CREATE INDEX ON {table} ({cols})")
    return commands


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


async def _read_until_ready(reader: asyncio.StreamReader) -> None:
    """Consume typed messages up to the next ReadyForQuery ('Z').

    Reads *through* the ReadyForQuery even on an ErrorResponse, so the connection
    is left clean for the next query, then raises the error text.
    """
    err = None
    while True:
        header = await reader.readexactly(5)
        body = await reader.readexactly(struct.unpack("!I", header[1:])[0] - 4)
        if header[:1] == b"E":
            err = body.decode("utf-8", "replace")
        if header[:1] == b"Z":
            if err is not None:
                raise RuntimeError(err)
            return


async def _distribute(commands: list[str]) -> None:
    """Run the pending distribution commands on a fresh side connection to PG.

    A separate connection (hence a separate transaction) is required: running
    create_distributed_table inside the CREATE's own transaction deadlocks. This
    fires after the client's CREATE commits, but the two happen on different
    connections, so it may briefly race ahead of the commit being visible; a
    "relation does not exist" is therefore retried a few times. Best effort: any
    other failure is logged, never surfaced to the client. Assumes trust auth on
    the loopback (the cluster's local auth), so there is no password step.
    """
    writer = None
    try:
        reader, writer = await asyncio.open_connection(PG_HOST, PG_PORT)
        params = (
            b"user\x00" + PG_USER.encode() + b"\x00"
            b"database\x00" + PG_DATABASE.encode() + b"\x00\x00"
        )
        writer.write(struct.pack("!II", len(params) + 8, STARTUP_PROTOCOL) + params)
        await writer.drain()
        await _read_until_ready(reader)  # auth (trust) + params -> ReadyForQuery
        for cmd in commands:
            q = cmd.encode("utf-8") + b"\x00"
            for attempt in range(8):
                writer.write(b"Q" + struct.pack("!I", len(q) + 4) + q)
                await writer.drain()
                try:
                    await _read_until_ready(reader)
                    break
                except RuntimeError as err:
                    # the client's CREATE hasn't become visible yet -> back off
                    if "does not exist" in str(err) and attempt < 7:
                        await asyncio.sleep(0.25)
                        continue
                    print(f"oblako: auto-distribute failed: {cmd}: {err}", flush=True)
                    break
    except Exception as exc:  # noqa: BLE001 - best effort, must not affect the client
        print(f"oblako: auto-distribute error: {exc!r}", flush=True)
    finally:
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()


def _rewrite_parameter_status(body: bytes) -> bytes:
    """Rewrite a ParameterStatus ('S') body if it reports server_version."""
    i = body.index(b"\x00")
    if body[:i] != b"server_version":
        return b"S" + struct.pack("!I", len(body) + 4) + body
    new = b"server_version\x00" + PROXY_SERVER_VERSION.encode() + b"\x00"
    return b"S" + struct.pack("!I", len(new) + 4) + new


async def _pipe_server(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    session: dict,
) -> None:
    """Server -> client, framed, for the Citus variant.

    Rewrites the server_version ParameterStatus (so clients see Redshift's version
    while the engine keeps its own), and watches ReadyForQuery: when one arrives
    with status 'I' (idle, i.e. the transaction just committed) and tables are
    pending distribution, it fires create_distributed_table on a side connection.
    Used only when PROXY_SERVER_VERSION or OBLAKO_CITUS is set; otherwise the
    single-node path is a raw byte copy (``_pipe_raw``).
    """
    try:
        while True:
            header = await reader.readexactly(5)  # type(1) + length(4)
            type_byte = header[:1]
            length = struct.unpack("!I", header[1:])[0]
            body = await reader.readexactly(length - 4)
            # Each ReadyForQuery completes one client statement, in order: dequeue
            # its distribution (if any) and hold it until the transaction commits
            # (status 'I'). Doing this synchronously here (before the drain below
            # yields to the other pipe) keeps the queue aligned with the RFQ stream.
            to_distribute = None
            if type_byte == b"Z" and OBLAKO_CITUS:
                if session["queue"]:
                    session["ready"].extend(session["queue"].pop(0))
                if body[:1] == b"I" and session["ready"]:
                    to_distribute, session["ready"] = session["ready"], []
            if type_byte == b"S" and PROXY_SERVER_VERSION:
                writer.write(_rewrite_parameter_status(body))
            else:
                writer.write(header + body)
            await writer.drain()
            if to_distribute:
                asyncio.create_task(_distribute(to_distribute))
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
        if length == 8 and struct.unpack("!I", body)[0] in (
            SSL_REQUEST,
            GSSENC_REQUEST,
        ):
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
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    session: dict,
) -> None:
    """Client -> server (post-startup): rewrite SQL in Q/P, relay the rest.

    On the Citus variant it also records the distribution each statement implies
    (from the original SQL, before DISTKEY is stripped), enqueued one entry per
    statement so it aligns 1:1 with the server's ReadyForQuery replies even when
    the client pipelines. ``_pipe_server`` dequeues and fires them on commit.
    """
    try:
        while True:
            type_byte = await reader.readexactly(1)
            length_b = await reader.readexactly(4)
            length = struct.unpack("!I", length_b)[0]
            body = await reader.readexactly(length - 4)
            if type_byte == b"Q":  # simple query: self-contained, one ReadyForQuery
                if OBLAKO_CITUS:
                    sql = body[:-1].decode("utf-8", "surrogatepass")
                    session["queue"].append(extract_distribution(sql))
                writer.write(_rewrite_query_message(body))
            elif type_byte == b"P":  # Parse: stage the distribution until its Sync
                if OBLAKO_CITUS:
                    i = body.index(b"\x00")
                    j = body.index(b"\x00", i + 1)
                    sql = body[i + 1 : j].decode("utf-8", "surrogatepass")
                    session["staged"] = extract_distribution(sql)
                writer.write(_rewrite_parse_message(body))
            else:
                if OBLAKO_CITUS and type_byte == b"S":  # Sync ends an extended stmt
                    session["queue"].append(session["staged"])
                    session["staged"] = []
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
    # Per-connection distribution state (Citus variant only): `queue` holds one
    # entry per client statement (a list of distribution/index commands, possibly
    # empty), `staged` is the extended-protocol statement's commands pending its
    # Sync, and `ready` accumulates them until the transaction commits.
    session: dict = {"queue": [], "staged": [], "ready": []}
    # server -> client: the framed path rewrites server_version and fires pending
    # distributions (Citus variant); the single-node path is a raw byte copy.
    if PROXY_SERVER_VERSION or OBLAKO_CITUS:
        server_to_client = _pipe_server(server_reader, client_writer, session)
    else:
        server_to_client = _pipe_raw(server_reader, client_writer)
    await asyncio.gather(
        _pipe_typed(client_reader, server_writer, session),
        server_to_client,
    )


async def main() -> None:
    """Run the proxy: listen for clients and relay each to the engine."""
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
