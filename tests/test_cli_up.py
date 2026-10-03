"""oblako up reports readiness and fails when a service never becomes ready.

A CI step that runs `oblako up <service>` must not pass while the service is
still starting, so the command exits 1 when any service is not ready.
"""

import argparse

import pytest

from oblako import cli


class FakeService:
    def __init__(self, name, ready):
        self.name = name
        self.ready = ready
        self.started = False
        self.timeout = None

    def start(self):
        self.started = True

    def wait_ready(self, timeout):
        self.timeout = timeout
        return self.ready


class FakeOblako:
    def __init__(self, services):
        self.services = services

    def up(self):
        for svc in self.services:
            svc.start()

    def wait_ready(self, timeout):
        return {svc.name: svc.wait_ready(timeout=timeout) for svc in self.services}


@pytest.fixture
def platform(monkeypatch):
    services = {}
    monkeypatch.setattr(cli, "_check_docker", lambda: None)
    monkeypatch.setattr(cli, "Oblako", lambda: FakeOblako(list(services.values())))
    monkeypatch.setattr(cli, "_get_service", lambda _o, name: services[name])
    return services


def up(service=None, timeout=120.0):
    cli.cmd_up(argparse.Namespace(service=service, timeout=timeout))


def test_one_ready_service(platform, capsys):
    platform["s3"] = FakeService("s3", ready=True)
    up("s3", timeout=7)
    assert platform["s3"].started
    assert platform["s3"].timeout == 7
    assert capsys.readouterr().out == "s3: ready\n"


def test_one_service_not_ready_exits_1(platform, capsys):
    platform["redshift"] = FakeService("redshift", ready=False)
    with pytest.raises(SystemExit) as exit_:
        up("redshift")
    assert exit_.value.code == 1
    assert "redshift: not ready" in capsys.readouterr().out


def test_all_services_exit_1_when_any_is_not_ready(platform, capsys):
    platform["s3"] = FakeService("s3", ready=True)
    platform["trino"] = FakeService("trino", ready=False)
    with pytest.raises(SystemExit) as exit_:
        up()
    assert exit_.value.code == 1
    out = capsys.readouterr().out
    assert "s3: ready" in out and "trino: not ready" in out


def test_all_services_ready(platform):
    platform["s3"] = FakeService("s3", ready=True)
    platform["dynamodb"] = FakeService("dynamodb", ready=True)
    up(timeout=30)
    assert all(svc.timeout == 30 for svc in platform.values())


def test_timeout_flag_parses(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "cmd_up", lambda args: seen.update(vars(args)))
    monkeypatch.setattr("sys.argv", ["oblako", "up", "s3", "--timeout", "300"])
    cli.main()
    assert seen["service"] == "s3" and seen["timeout"] == 300.0


def test_port_in_use_is_a_message_not_a_trace(platform, capsys):
    class Blocked(FakeService):
        def start(self):
            raise cli.PortInUseError("host port 9200 is already in use")

    platform["opensearch"] = Blocked("opensearch", ready=False)
    with pytest.raises(SystemExit) as exit_:
        up("opensearch")
    assert exit_.value.code == 1
    assert capsys.readouterr().out == ("Error: host port 9200 is already in use\n")
