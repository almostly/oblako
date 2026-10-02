"""Real PostgreSQL instances behind the RDS control plane.

Each standalone PostgreSQL DB instance created through the rds-control proxy is
its own container (the pgvector image RdsService runs), listening inside the
container on the same port it publishes on the host. Every instance joins one
Docker network, ``oblako-rds``, under its endpoint name
``<id>.<region>.rds.localhost``. macOS and systemd resolve any ``*.localhost``
name to the loopback address, so that one ``host:port`` reaches the instance from
the host, and from another instance's container, which is what a read replica's
base backup or a logical-replication subscription needs.

* A read replica is a physical streaming standby: ``pg_basebackup -R`` from the
  source, then a hot standby. ``promote`` runs ``pg_promote()``.
* ``wal_level`` follows the parameter group, as on RDS: ``rds.logical_replication
  = 1`` gives ``logical`` (applied when the instance starts or reboots).

Creating, rebooting and promoting take seconds, so they run in a thread and the
record's ``status`` says where they are (``creating``, ``rebooting``,
``available``, ``failed``), which DescribeDBInstances reports, as on RDS.

State (port, master user, role, status) lives in ``~/.oblako/rds/instances.json``;
the containers are labelled ``oblako.rds.instance`` too.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

STATE = Path.home() / ".oblako" / "rds" / "instances.json"
INITDB = Path.home() / ".oblako" / "rds" / "initdb"
NETWORK = "oblako-rds"
PGDATA = "/var/lib/postgresql/data"

# Allow replication connections: a standby's base backup and streaming, and a
# logical subscriber's initial copy. Runs once, when the data directory is created.
_INIT_SCRIPT = """#!/bin/bash
echo "host replication all all scram-sha-256" >> "$PGDATA/pg_hba.conf"
"""

# A standby clones its source once, then runs as a hot standby on its own port.
_REPLICA_ENTRYPOINT = """set -e
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA" && chmod 700 "$PGDATA"
  gosu postgres pg_basebackup -h "$SOURCE_HOST" -p "$SOURCE_PORT" -U "$SOURCE_USER" \\
    -D "$PGDATA" -R -X stream
fi
exec gosu postgres postgres -c port="$PORT" -c hot_standby=on \\
  -c max_wal_senders=10 -c max_replication_slots=10
"""

_lock = threading.RLock()


def _docker():
    from oblako.services.backends import docker_client

    return docker_client()


def _image() -> str:
    from oblako.services.rds import POSTGRES_IMAGE

    return POSTGRES_IMAGE


def endpoint_address(instance_id: str, region: str) -> str:
    """Return the instance's endpoint name: resolvable on the host and in the network."""
    return f"{instance_id}.{region}.rds.localhost"


def container_name(instance_id: str) -> str:
    """Return the instance's container name."""
    return f"oblako-rds-{instance_id}"


def _volume(instance_id: str) -> str:
    return f"oblako-rds-{instance_id}-data"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load() -> dict[str, dict]:
    """Return every instance record, keyed by DB instance identifier."""
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {}


def get(instance_id: str) -> dict | None:
    """Return one instance record, or None if oblako runs no container for it."""
    return load().get(instance_id)


def _save(records: dict[str, dict]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=1))
    tmp.replace(STATE)


def _update(instance_id: str, **fields) -> dict:
    """Merge ``fields`` into the instance's record and return it."""
    with _lock:
        records = load()
        record = {**records.get(instance_id, {}), **fields}
        records[instance_id] = record
        _save(records)
        return record


def _drop(instance_id: str) -> None:
    with _lock:
        records = load()
        records.pop(instance_id, None)
        _save(records)


def _in_background(instance_id: str, work, *args) -> None:
    """Run ``work`` in a thread; mark the instance available, or failed with the error."""

    def run():
        try:
            work(instance_id, *args)
            if get(instance_id) is not None:
                _update(instance_id, status="available", error=None)
            else:  # deleted while it was starting: remove what the work created
                _remove(instance_id)
        except Exception as e:
            if get(instance_id) is not None:
                _update(instance_id, status="failed", error=str(e))

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Docker plumbing
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _network(client):
    from docker.errors import NotFound

    try:
        return client.networks.get(NETWORK)
    except NotFound:
        return client.networks.create(NETWORK, driver="bridge")


def _init_dir() -> Path:
    INITDB.mkdir(parents=True, exist_ok=True)
    script = INITDB / "10-replication.sh"
    script.write_text(_INIT_SCRIPT)
    script.chmod(0o755)
    return INITDB


def _remove_container(client, name: str) -> None:
    from docker.errors import NotFound

    try:
        client.containers.get(name).remove(force=True)
    except NotFound:
        pass


def _run(instance_id: str, record: dict, **spec):
    """Create the container on the oblako-rds network under its endpoint name, start it."""
    client = _docker()
    image = _image()
    try:
        client.images.get(image)
    except Exception:
        client.images.pull(image)
    network = _network(client)
    name = container_name(instance_id)
    _remove_container(client, name)
    port = record["port"]
    container = client.containers.create(
        image,
        name=name,
        ports={f"{port}/tcp": port},
        labels={
            "oblako.service": "rds-instance",
            "oblako.rds.instance": instance_id,
        },
        **spec,
    )
    network.connect(
        container, aliases=[endpoint_address(instance_id, record["region"])]
    )
    container.start()
    _wait_ready(container, port)
    return container


def _wait_ready(container, port: int, timeout: float = 180.0) -> None:
    """Wait for the server on its TCP port (the image's init server listens on a socket only)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        container.reload()
        if container.status == "exited":
            logs = container.logs(tail=20).decode(errors="replace")
            raise RuntimeError(f"{container.name} exited:\n{logs}")
        probe = container.exec_run(["pg_isready", "-h", "127.0.0.1", "-p", str(port)])
        if probe.exit_code == 0:
            return
        time.sleep(0.5)
    raise TimeoutError(f"{container.name} did not become ready on port {port}")


def _psql(instance_id: str, record: dict, sql: str) -> str:
    container = _docker().containers.get(container_name(instance_id))
    result = container.exec_run(
        [
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            record["user"],
            "-p",
            str(record["port"]),
            "-d",
            record["database"],
            "-Atc",
            sql,
        ],
        environment={"PGPASSWORD": record["password"]},
    )
    output = result.output.decode(errors="replace").strip()
    if result.exit_code != 0:
        raise RuntimeError(output)
    return output


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------
def _primary_spec(instance_id: str, record: dict) -> dict:
    port = record["port"]
    wal_level = "logical" if record["logical"] else "replica"
    return {
        "command": [
            "postgres",
            "-c",
            f"port={port}",
            "-c",
            f"wal_level={wal_level}",
            "-c",
            "max_wal_senders=10",
            "-c",
            "max_replication_slots=10",
        ],
        "environment": {
            "POSTGRES_USER": record["user"],
            "POSTGRES_PASSWORD": record["password"],
            "POSTGRES_DB": record["database"],
        },
        "volumes": {
            _volume(instance_id): {"bind": PGDATA, "mode": "rw"},
            str(_init_dir()): {"bind": "/docker-entrypoint-initdb.d", "mode": "ro"},
        },
    }


def _replica_spec(instance_id: str, record: dict) -> dict:
    source = get(record["source"])
    if source is None:
        raise RuntimeError(f"source instance {record['source']} is gone")
    return {
        "entrypoint": ["bash", "-c", _REPLICA_ENTRYPOINT],
        "environment": {
            "PGDATA": PGDATA,
            "PORT": str(record["port"]),
            "SOURCE_HOST": endpoint_address(record["source"], source["region"]),
            "SOURCE_PORT": str(source["port"]),
            "SOURCE_USER": source["user"],
            "PGPASSWORD": source["password"],
        },
        "volumes": {_volume(instance_id): {"bind": PGDATA, "mode": "rw"}},
    }


def _start(instance_id: str) -> None:
    record = get(instance_id)
    if record is None:
        return
    if record["role"] == "replica":
        _run(instance_id, record, **_replica_spec(instance_id, record))
    else:
        _run(instance_id, record, **_primary_spec(instance_id, record))
    # Report the version the engine really runs, as RDS reports the minor version
    version = _psql(instance_id, record, "SHOW server_version").split()[0]
    _update(instance_id, engine_version=version)


# ---------------------------------------------------------------------------
# Lifecycle (called by the proxy)
# ---------------------------------------------------------------------------
def create_primary(
    instance_id: str,
    *,
    user: str,
    password: str,
    database: str,
    region: str,
    logical: bool,
) -> dict:
    """Record a new primary instance and start its container in the background."""
    record = _update(
        instance_id,
        port=_free_port(),
        region=region,
        user=user,
        password=password,
        database=database,
        role="primary",
        logical=logical,
        source=None,
        promoted=False,
        status="creating",
        error=None,
    )
    _in_background(instance_id, _start)
    return record


def create_replica(instance_id: str, source_id: str) -> dict:
    """Record a streaming read replica of ``source_id`` and start it in the background."""
    source = get(source_id)
    if source is None:
        raise KeyError(source_id)
    record = _update(
        instance_id,
        **{k: source[k] for k in ("user", "password", "database", "region")},
        port=_free_port(),
        role="replica",
        logical=False,
        source=source_id,
        promoted=False,
        status="creating",
        error=None,
    )
    _in_background(instance_id, _start)
    return record


def _promote(instance_id: str) -> None:
    record = get(instance_id)
    if record is None:
        return
    _psql(instance_id, record, "SELECT pg_promote(wait => true)")
    # From now on the instance restarts as a primary, on its own data directory.
    _update(instance_id, role="primary", source=None, promoted=True)


def promote(instance_id: str) -> None:
    """Promote a read replica to a standalone instance (pg_promote), in the background."""
    record = get(instance_id)
    if record is None or record["role"] != "replica":
        return
    _update(instance_id, status="modifying")
    _in_background(instance_id, _promote)


def reboot(instance_id: str, logical: bool) -> None:
    """Restart an instance in the background, applying the parameter group's wal_level."""
    record = get(instance_id)
    if record is None:
        return
    if record["role"] == "primary":
        _update(instance_id, logical=logical)
    _update(instance_id, status="rebooting")
    # wal_level is a startup setting: recreate the container on the same volume
    _in_background(instance_id, _start)


def delete(instance_id: str) -> None:
    """Remove an instance's container, its data volume and its record."""
    _drop(instance_id)
    _remove(instance_id)


def _remove(instance_id: str) -> None:
    from docker.errors import NotFound

    client = _docker()
    _remove_container(client, container_name(instance_id))
    try:
        client.volumes.get(_volume(instance_id)).remove(force=True)
    except NotFound:
        pass
