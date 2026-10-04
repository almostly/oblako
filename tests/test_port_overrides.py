"""Unit tests: OBLAKO_PORT_<NAME> moves a service off its default port.

Each case imports oblako.ports in a fresh interpreter, since the overrides apply at
import time, as they do in every oblako process.
"""

import subprocess
import sys


def _ports(env: dict[str, str], code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", f"from oblako import ports; {code}"],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
    )


def test_override_moves_the_port_everywhere_it_is_read():
    # the RDS service's default and the RDS Data API's target both follow it
    run = _ports(
        {"OBLAKO_PORT_RDS_PG": "5433"},
        "from oblako.services import rds;"
        "print(ports.RDS_PG, rds._ENGINES['postgres']['default_host_port'])",
    )
    assert run.stdout.split() == ["5433", "5433"]


def test_defaults_without_overrides():
    run = _ports({}, "print(ports.RDS_PG, ports.S3, ports.DYNAMODB)")
    assert run.stdout.split() == ["5432", "9000", "8001"]


def test_unknown_name_fails_loudly():
    run = _ports({"OBLAKO_PORT_RDS_PGG": "5433"}, "pass")
    assert run.returncode != 0
    assert "no port named RDS_PGG" in run.stderr


def test_bad_value_fails_loudly():
    run = _ports({"OBLAKO_PORT_S3": "ninety"}, "pass")
    assert run.returncode != 0
    assert "is not a port number" in run.stderr


def test_name_of_maps_a_port_back_to_its_registry_name():
    from oblako import ports

    assert ports.name_of(ports.RDS_PG) == "RDS_PG"
    assert ports.name_of(1) is None
