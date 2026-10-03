"""Redshift control-plane proxy over moto that runs real clusters.

moto serves the Redshift API (clusters, parameter groups, snapshots), but its
clusters are metadata whose endpoints point at nothing. This proxy forwards every
call to moto and makes the clusters real:

* A **single-node** cluster's endpoint is oblako's shared Redshift engine,
  ``localhost:5439``.
* A **multi-node** cluster (``ClusterType='multi-node'``) runs as its own Citus
  cluster, a leader node plus ``NumberOfNodes`` compute nodes (see ``clusters``),
  at ``<id>.<region>.redshift.localhost`` on a port of its own.
  ``DescribeClusters`` reports its real status (``creating`` until every node is
  registered) and ``ClusterNodes`` with each node's role and address.
* ``RebootCluster`` restarts the nodes; ``DeleteCluster`` removes them and their
  data.
"""

from __future__ import annotations

import os
import re
import uuid
from urllib.parse import parse_qs

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from oblako import ports

from . import clusters

_NS = "http://redshift.amazonaws.com/doc/2012-12-01/"
_CREDENTIAL = re.compile(r"Credential=[^/]+/\d{8}/([a-z0-9-]+)/")
_CLUSTER = re.compile(r"<Cluster>(.*?)</Cluster>", re.DOTALL)
_ENDPOINT = re.compile(r"<Endpoint>.*?</Endpoint>", re.DOTALL)
_STATUS = re.compile(r"<ClusterStatus>[^<]*</ClusterStatus>")
_NODES = re.compile(r"<ClusterNodes>.*?</ClusterNodes>", re.DOTALL)
MAX_NODES = 8  # compute nodes oblako starts for one cluster


def _moto_url() -> str:
    return os.environ.get("OBLAKO_MOTO_ENDPOINT") or f"http://localhost:{ports.MOTO}"


def _tag(xml: str, name: str) -> str | None:
    m = re.search(rf"<{name}>([^<]*)</{name}>", xml)
    return m.group(1) if m else None


def region_of(request_headers) -> str:
    """Return the region from the SigV4 credential scope, else the default region."""
    m = _CREDENTIAL.search(request_headers.get("authorization", ""))
    return m.group(1) if m else os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def error_response(code: str, message: str, status: int = 400) -> Response:
    """Return a Redshift query-protocol error."""
    body = (
        f'<ErrorResponse xmlns="{_NS}"><Error><Type>Sender</Type>'
        f"<Code>{code}</Code><Message>{message}</Message></Error>"
        f"<RequestId>{uuid.uuid4()}</RequestId></ErrorResponse>"
    )
    return Response(body, status_code=status, media_type="text/xml")


# ---------------------------------------------------------------------------
# Response rewriting
# ---------------------------------------------------------------------------
def _endpoint(address: str, port: int) -> str:
    return f"<Endpoint><Address>{address}</Address><Port>{port}</Port></Endpoint>"


def _cluster_nodes(ips: dict[str, str]) -> str:
    members = "".join(
        f"<member><NodeRole>{role}</NodeRole>"
        f"<PrivateIPAddress>{ip}</PrivateIPAddress>"
        f"<PublicIPAddress>127.0.0.1</PublicIPAddress></member>"
        for role, ip in ips.items()
    )
    return f"<ClusterNodes>{members}</ClusterNodes>"


def _set(block: str, pattern: re.Pattern, value: str) -> str:
    if pattern.search(block):
        return pattern.sub(value, block, count=1)
    return block + value


def _rewrite_cluster(block: str, records: dict[str, dict]) -> str:
    cluster_id = _tag(block, "ClusterIdentifier") or ""
    record = records.get(cluster_id)
    if record is None:
        # a single-node cluster is the shared engine
        if (_tag(block, "NumberOfNodes") or "1") == "1":
            block = _set(block, _ENDPOINT, _endpoint("localhost", ports.REDSHIFT_PG))
        return block
    address = clusters.endpoint_address(cluster_id, record["region"])
    block = _set(block, _ENDPOINT, _endpoint(address, record["port"]))
    status = f"<ClusterStatus>{record['status']}</ClusterStatus>"
    block = _STATUS.sub(status, block, count=1)
    if record.get("node_ips"):
        block = _set(block, _NODES, _cluster_nodes(record["node_ips"]))
    return block


def rewrite(xml: str, records: dict[str, dict]) -> str:
    """Point every cluster at the engine that serves it."""
    if "<Cluster>" not in xml:
        return xml
    return _CLUSTER.sub(
        lambda m: f"<Cluster>{_rewrite_cluster(m.group(1), records)}</Cluster>", xml
    )


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------
class RedshiftControlProxy:
    """Forwards Redshift calls to moto and runs multi-node clusters for them."""

    def __init__(self, backend_url: str | None = None):
        """Bind to the moto endpoint."""
        self.backend = (backend_url or _moto_url()).rstrip("/")

    async def _forward(self, request: Request, body: bytes) -> httpx.Response:
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length")
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await client.request(
                request.method,
                self.backend + request.url.path,
                params=request.query_params,
                content=body,
                headers=headers,
            )

    @staticmethod
    def _multi_node(form: dict[str, str]) -> bool:
        return (
            form.get("ClusterType", "multi-node") == "multi-node"
            and int(form.get("NumberOfNodes", "1")) > 1
        )

    def _precheck(self, action: str, form: dict[str, str]) -> Response | None:
        """Refuse what Redshift refuses, and clusters too large for one machine."""
        if action == "CreateCluster" and self._multi_node(form):
            nodes = int(form.get("NumberOfNodes", "2"))
            if nodes > MAX_NODES:
                return error_response(
                    "InvalidParameterValue",
                    f"oblako runs at most {MAX_NODES} compute nodes per cluster.",
                )
        if action in ("RebootCluster", "DeleteCluster"):
            record = clusters.get(form.get("ClusterIdentifier", ""))
            if record is not None and record["status"] not in ("available", "failed"):
                return error_response(
                    "InvalidClusterState",
                    f"There is an operation running on the Cluster. "
                    f"Please try to {action[:-7].lower()} it at a later time.",
                )
        return None

    def _apply(self, action: str, form: dict[str, str], region: str) -> None:
        """Do to the real clusters what moto just accepted."""
        cluster_id = form.get("ClusterIdentifier", "")
        if action == "CreateCluster" and self._multi_node(form):
            clusters.create(
                cluster_id,
                nodes=int(form["NumberOfNodes"]),
                user=form["MasterUsername"],
                password=form.get("MasterUserPassword", ""),
                database=form.get("DBName") or "dev",
                region=region,
            )
        elif action == "RebootCluster":
            clusters.reboot(cluster_id)
        elif action == "DeleteCluster" and clusters.get(cluster_id) is not None:
            clusters.delete(cluster_id)

    async def handle(self, request: Request) -> Response:
        """Forward one Redshift call to moto, act on it, and rewrite its endpoints."""
        body = await request.body()
        form = {k: v[0] for k, v in parse_qs(body.decode(errors="replace")).items()}
        action = form.get("Action", "")
        refused = self._precheck(action, form)
        if refused is not None:
            return refused
        upstream = await self._forward(request, body)
        if upstream.status_code == 200:
            try:
                region = region_of(request.headers)
                await run_in_threadpool(self._apply, action, form, region)
            except Exception as e:
                return error_response("InternalFailure", str(e), status=500)
        content = upstream.content
        if upstream.status_code == 200 and b"<Cluster>" in content:
            content = rewrite(content.decode(), clusters.load()).encode()
        return Response(
            content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/xml"),
        )


def create_app(backend_url: str | None = None) -> Starlette:
    """Build the proxy's ASGI app."""
    proxy = RedshiftControlProxy(backend_url)
    return Starlette(
        routes=[Route("/{path:path}", proxy.handle, methods=["GET", "POST"])]
    )


app = create_app()
