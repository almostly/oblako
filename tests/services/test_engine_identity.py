"""Engines recognize their own server and refuse a port another process holds."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from oblako.engines import cloudformation
from oblako.engines.identity import claim_port, is_engine


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def foreign_server():
    """A non-oblako server that answers 200 on / (like OpenSearch Dashboards)."""

    class Ok(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"not oblako")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", _free_port()), Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_engine_recognizes_its_own_server():
    port = _free_port()
    cloudformation.start_in_thread(port=port)
    assert is_engine(port, "cloudformation")
    assert cloudformation.is_running(port)
    # the header names one engine, so another engine doesn't claim it
    assert not is_engine(port, "sagemaker")


def test_a_foreign_server_is_not_mistaken_for_the_engine(foreign_server):
    assert not cloudformation.is_running(foreign_server)


def test_start_refuses_a_port_another_process_holds(foreign_server):
    with pytest.raises(RuntimeError, match=f"port {foreign_server} is in use"):
        cloudformation.start_in_thread(port=foreign_server)


def test_claim_port_passes_on_a_free_port():
    claim_port(_free_port(), "cloudformation")
