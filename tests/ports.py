"""Free TCP ports for engines a test starts in-process."""

import socket


def free_port() -> int:
    """Return a port nothing listens on, so a test never takes a canonical oblako port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
