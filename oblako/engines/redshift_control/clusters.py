"""Real multi-node Redshift clusters behind the Redshift control plane.

A cluster created with ``ClusterType='multi-node'`` runs as a Citus cluster: a
leader node (the Citus coordinator, which holds no data, as Redshift's leader
holds none) and ``NumberOfNodes`` compute nodes (Citus workers that hold the
shards). Every node runs the redshift-cluster image, the single-node engine's
Redshift compatibility layer with Citus underneath, so ``DISTKEY`` tables are
distributed across the compute nodes and ``DISTSTYLE ALL`` tables are copied to
every node.

The nodes share one Docker network, ``oblako-redshift``. The leader joins it
under the cluster's endpoint name, ``<id>.<region>.redshift.localhost``, which
also resolves to your machine, and publishes Redshift's port on a free host
port. Nodes authenticate to each other with the master user's password.

Starting a cluster takes a few seconds, so it runs in a thread and the record's
``status`` (``creating``, ``rebooting``, ``available``, ``failed``) is what
DescribeClusters reports. State lives in ``~/.oblako/redshift/clusters.json``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

from oblako import ports
from oblako.services.backends import publish

STATE = Path.home() / ".oblako" / "redshift" / "clusters.json"
NETWORK = "oblako-redshift"
IMAGE = "public.ecr.aws/oblako/redshift-cluster:16"
BASE_IMAGE = "public.ecr.aws/oblako/redshift-local:16"
PGDATA = "/var/lib/postgresql/data"
INTERNAL_PG_PORT = 5433  # PostgreSQL inside each node; the proxy listens on 5439

_lock = threading.RLock()


def _docker():
    from oblako.services.backends import docker_client

    return docker_client()


def endpoint_address(cluster_id: str, region: str) -> str:
    """Return the cluster's endpoint name: resolvable on the host and in the network."""
    return f"{cluster_id}.{region}.redshift.localhost"


def node_name(cluster_id: str, node: int | None) -> str:
    """Return a node's container name: the leader, or compute node ``node``."""
    suffix = "leader" if node is None else f"compute-{node}"
    return f"oblako-redshift-{cluster_id}-{suffix}"


def _compute_alias(cluster_id: str, node: int) -> str:
    return f"{cluster_id}-compute-{node}"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load() -> dict[str, dict]:
    """Return every cluster record, keyed by cluster identifier."""
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {}


def get(cluster_id: str) -> dict | None:
    """Return one cluster record, or None if oblako runs no nodes for it."""
    return load().get(cluster_id)


def _save(records: dict[str, dict]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=1))
    tmp.replace(STATE)


def _update(cluster_id: str, create: bool = False, **fields) -> dict:
    """Merge ``fields`` into the cluster's record and return it.

    Only ``create`` adds a record: a background step finishing after the cluster
    was deleted must not bring it back.
    """
    with _lock:
        records = load()
        if cluster_id not in records and not create:
            return {}
        record = {**records.get(cluster_id, {}), **fields}
        records[cluster_id] = record
        _save(records)
        return record


def _drop(cluster_id: str) -> None:
    with _lock:
        records = load()
        records.pop(cluster_id, None)
        _save(records)


def _in_background(cluster_id: str, work) -> None:
    """Run ``work`` in a thread; mark the cluster available, or failed with the error."""

    def run():
        try:
            work(cluster_id)
            if get(cluster_id) is not None:
                _update(cluster_id, status="available", error=None)
            else:  # deleted while it was starting
                _remove(cluster_id)
        except Exception as e:
            if get(cluster_id) is not None:
                _update(cluster_id, status="failed", error=str(e))

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


def _ensure_image(client) -> None:
    """Use the local image, else pull it, else build it from oblako's sources."""
    from docker.errors import APIError, ImageNotFound

    try:
        client.images.get(IMAGE)
        return
    except ImageNotFound:
        pass
    try:
        client.images.pull(IMAGE)
        return
    except APIError:
        pass
    images = Path(__file__).resolve().parents[2] / "images"
    try:
        client.images.get(BASE_IMAGE)
    except ImageNotFound:
        client.images.build(path=str(images / "redshift"), tag=BASE_IMAGE, rm=True)
    client.images.build(
        path=str(images / "redshift-cluster"),
        tag=IMAGE,
        buildargs={"BASE_IMAGE": BASE_IMAGE},
        rm=True,
    )


def _remove_container(client, name: str) -> None:
    from docker.errors import NotFound

    try:
        client.containers.get(name).remove(force=True, v=True)
    except NotFound:
        pass


def _run_node(client, network, cluster_id: str, record: dict, node: int | None):
    """Create and start one node on the cluster network."""
    name = node_name(cluster_id, node)
    _remove_container(client, name)
    environment = {
        "POSTGRES_USER": record["user"],
        "POSTGRES_PASSWORD": record["password"],
        "POSTGRES_DB": record["database"],
        "POSTGRES_HOST_AUTH_METHOD": "md5",
        # COPY and UNLOAD reach oblako's S3 on the host, as on the single node
        "OBLAKO_S3_ENDPOINT": f"http://host.docker.internal:{ports.S3}",
    }
    from oblako.services.redshift import CERT_DIR, ensure_cert

    ensure_cert()  # every node presents this machine's cert (`oblako trust`)
    volumes = {
        f"{name}-data": {"bind": PGDATA, "mode": "rw"},
        str(CERT_DIR): {"bind": "/etc/oblako-redshift", "mode": "ro"},
    }
    leader_alias = endpoint_address(cluster_id, record["region"])
    computes = [_compute_alias(cluster_id, n) for n in range(record["nodes"])]
    if node is None:
        environment.update(
            OBLAKO_CITUS_ROLE="coordinator",
            OBLAKO_CITUS_COORDINATOR_HOST=leader_alias,
            OBLAKO_CITUS_WORKERS=",".join(computes),
        )
        published = {"5439/tcp": record["port"]}
        aliases = [leader_alias]
        # Redshift ML trains CREATE MODELs in containers on the host daemon
        volumes["/var/run/docker.sock"] = {
            "bind": "/var/run/docker.sock",
            "mode": "rw",
        }
    else:
        environment["OBLAKO_CITUS_ROLE"] = "worker"
        published = {}
        aliases = [computes[node]]
    container = client.containers.create(
        IMAGE,
        name=name,
        environment=environment,
        ports=publish(published, client),
        volumes=volumes,
        extra_hosts={"host.docker.internal": "host-gateway"},
        labels={
            "oblako.service": "redshift-node",
            "oblako.redshift.cluster": cluster_id,
        },
    )
    network.connect(container, aliases=aliases)
    container.start()
    return container


def _wait_ready(cluster_id: str, record: dict, timeout: float = 300.0) -> None:
    """Wait until the leader has registered every compute node."""
    client = _docker()
    leader = client.containers.get(node_name(cluster_id, None))
    query = "SELECT count(*) FROM pg_dist_node WHERE noderole = 'primary'"
    deadline = time.time() + timeout
    while time.time() < deadline:
        leader.reload()
        if leader.status == "exited":
            logs = leader.logs(tail=20).decode(errors="replace")
            raise RuntimeError(f"{leader.name} exited:\n{logs}")
        probe = leader.exec_run(
            [
                "psql",
                "-h",
                "127.0.0.1",
                "-p",
                str(INTERNAL_PG_PORT),
                "-U",
                record["user"],
                "-d",
                record["database"],
                "-Atc",
                query,
            ],
            environment={"PGPASSWORD": record["password"]},
        )
        registered = probe.output.decode(errors="replace").strip()
        if probe.exit_code == 0 and registered == str(record["nodes"] + 1):
            with socket.socket() as s:
                s.settimeout(1)
                if s.connect_ex(("127.0.0.1", record["port"])) == 0:
                    return
        time.sleep(1)
    raise TimeoutError(f"cluster {cluster_id} did not become ready")


def _start(cluster_id: str) -> None:
    record = get(cluster_id)
    if record is None:
        return
    client = _docker()
    _ensure_image(client)
    network = _network(client)
    for node in range(record["nodes"]):
        _run_node(client, network, cluster_id, record, node)
    _run_node(client, network, cluster_id, record, None)
    _wait_ready(cluster_id, record)
    ips = {}
    for node in [None, *range(record["nodes"])]:
        container = client.containers.get(node_name(cluster_id, node))
        container.reload()
        settings = container.attrs["NetworkSettings"]["Networks"][NETWORK]
        ips["LEADER" if node is None else f"COMPUTE-{node}"] = settings["IPAddress"]
    _update(cluster_id, node_ips=ips)


def _remove(cluster_id: str) -> None:
    client = _docker()
    for container in client.containers.list(
        all=True, filters={"label": f"oblako.redshift.cluster={cluster_id}"}
    ):
        container.remove(force=True, v=True)
    for volume in client.volumes.list(
        filters={"name": f"oblako-redshift-{cluster_id}-"}
    ):
        volume.remove(force=True)


# ---------------------------------------------------------------------------
# Lifecycle (called by the proxy)
# ---------------------------------------------------------------------------
def create(
    cluster_id: str,
    *,
    nodes: int,
    user: str,
    password: str,
    database: str,
    region: str,
) -> dict:
    """Record a new multi-node cluster and start its nodes in the background."""
    record = _update(
        cluster_id,
        create=True,
        port=_free_port(),
        nodes=nodes,
        user=user,
        password=password,
        database=database,
        region=region,
        status="creating",
        error=None,
        node_ips={},
    )
    _in_background(cluster_id, _start)
    return record


def reboot(cluster_id: str) -> None:
    """Restart every node of a cluster, in the background."""
    if get(cluster_id) is None:
        return
    _update(cluster_id, status="rebooting")

    def work(cid: str) -> None:
        record = get(cid)
        if record is None:
            return
        client = _docker()
        for node in [*range(record["nodes"]), None]:
            client.containers.get(node_name(cid, node)).restart()
        _wait_ready(cid, record)

    _in_background(cluster_id, work)


def delete(cluster_id: str) -> None:
    """Remove a cluster's nodes, their data and the record."""
    _drop(cluster_id)
    _remove(cluster_id)
