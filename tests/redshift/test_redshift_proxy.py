"""Tests for the Redshift wire proxy (oblako.engines.redshift_proxy).

The pure-protocol tests run anywhere. The roundtrip test needs the redshift
engine up (`make up`, or `docker compose up redshift`) and the redshift-connector
driver — it proves redshift-connector (the driver behind dbt-redshift), which
can't talk to pgredshift directly, connects through the proxy on Redshift's
port (5439).
"""

import struct

import pytest

from oblako.engines import redshift_proxy as proxy


def _startup(params: dict[str, str]) -> bytes:
    """Build a Postgres StartupMessage body (version + null-terminated pairs)."""
    body = struct.pack("!I", proxy._PROTOCOL_3_0)
    for k, v in params.items():
        body += k.encode() + b"\x00" + v.encode() + b"\x00"
    return body + b"\x00"


# ---------------------------------------------------------------------------
# Protocol rewriting (no Docker / driver needed)
# ---------------------------------------------------------------------------
def test_rewrite_startup_drops_redshift_only_keys_and_flags_driver():
    body = _startup(
        {
            "user": "oblako",
            "database": "oblako",
            "client_protocol_version": "2",
            "driver_version": "redshift_connector 2.1.15",
            "os_version": "Darwin",
            "driver_discovery_version": "1",
        }
    )
    out, is_redshift_driver = proxy._rewrite_startup(body)

    assert is_redshift_driver is True
    # Length prefix is correct and the Redshift-only keys are gone…
    assert struct.unpack("!I", out[:4])[0] == len(out)
    for dropped in (b"client_protocol_version", b"driver_version", b"os_version"):
        assert dropped not in out
    # …while the parameters Postgres needs survive.
    assert b"user\x00oblako\x00" in out
    assert b"database\x00oblako\x00" in out


def test_rewrite_startup_passes_psycopg2_through_unflagged():
    body = _startup(
        {"user": "oblako", "database": "oblako", "application_name": "psql"}
    )
    out, is_redshift_driver = proxy._rewrite_startup(body)

    assert is_redshift_driver is False  # no Redshift-only keys -> not the driver
    assert b"user\x00oblako\x00" in out
    assert b"application_name\x00psql\x00" in out


def test_rewrite_server_version_overrides_only_that_key():
    body = b"server_version\x0010.18 (Debian 10.18-1.pgdg100+1)\x00"
    assert proxy._rewrite_server_version(body) == b"server_version\x008.0.2\x00"
    # Any other ParameterStatus is left alone (returns None -> forwarded as-is).
    assert proxy._rewrite_server_version(b"client_encoding\x00UTF8\x00") is None


# ---------------------------------------------------------------------------
# End-to-end through the proxy (requires redshift engine + driver)
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_redshift_connector_connects_through_proxy():
    redshift_connector = pytest.importorskip("redshift_connector")

    try:
        proxy.start_in_thread()  # uses the running endpoint if already up
    except RuntimeError:
        pytest.skip("redshift engine not reachable")

    common = dict(database="oblako", user="oblako", password="oblako", ssl=False)

    # Direct to the raw pgredshift engine: the driver's startup params are rejected.
    with pytest.raises(
        redshift_connector.error.ProgrammingError, match="client_protocol_version"
    ):
        redshift_connector.connect(
            host="localhost", port=proxy.DEFAULT_BACKEND_PORT, **common
        )

    # Through the endpoint: connects, and sees a Redshift-shaped server_version.
    conn = redshift_connector.connect(
        host="localhost", port=proxy.DEFAULT_LISTEN_PORT, **common
    )
    try:
        assert str(conn._server_version) == "8.0.2"
        cur = conn.cursor()
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()
