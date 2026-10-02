"""RDS control-plane proxy over moto that runs a real PostgreSQL per DB instance.

moto serves the RDS API (instances, clusters, parameter groups, snapshots) but its
instances are metadata, every endpoint pointing at nothing. This proxy forwards
every call to moto and, for standalone PostgreSQL instances, does what RDS does:

* ``CreateDBInstance`` starts the instance's own PostgreSQL container.
* ``CreateDBInstanceReadReplica`` starts a streaming standby of its source.
* ``PromoteReadReplica`` promotes the standby to a standalone instance.
* ``RebootDBInstance`` restarts it, applying the parameter group's
  ``rds.logical_replication`` (``wal_level=logical``), as RDS does on reboot.
* ``DeleteDBInstance`` removes the container and its data.

Responses are rewritten so each instance's ``Endpoint`` is the real one
(``<id>.<region>.rds.localhost`` and its port) and ``DBInstanceStatus`` follows the
container (``creating`` until it accepts connections). Aurora cluster members and
other engines pass through to moto unchanged.
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

from . import instances

_NS = "http://rds.amazonaws.com/doc/2014-10-31/"
_CREDENTIAL = re.compile(r"Credential=[^/]+/\d{8}/([a-z0-9-]+)/")
_INSTANCE = re.compile(r"<DBInstance>(.*?)</DBInstance>", re.DOTALL)
_ENDPOINT = re.compile(r"<Endpoint>.*?</Endpoint>", re.DOTALL)
_STATUS = re.compile(r"<DBInstanceStatus>[^<]*</DBInstanceStatus>")
_VERSION = re.compile(r"<EngineVersion>[^<]*</EngineVersion>")
_SOURCE = re.compile(
    r"<ReadReplicaSourceDBInstanceIdentifier>[^<]*</ReadReplicaSourceDBInstanceIdentifier>"
)
_REPLICA = re.compile(r"<(ReadReplicaDBInstanceIdentifier|member)>([^<]*)</\1>")
_REPLICAS = re.compile(
    r"<ReadReplicaDBInstanceIdentifiers>(.*?)</ReadReplicaDBInstanceIdentifiers>",
    re.DOTALL,
)


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
    """Return an RDS query-protocol error."""
    body = (
        f'<ErrorResponse xmlns="{_NS}"><Error><Type>Sender</Type>'
        f"<Code>{code}</Code><Message>{message}</Message></Error>"
        f"<RequestId>{uuid.uuid4()}</RequestId></ErrorResponse>"
    )
    return Response(body, status_code=status, media_type="text/xml")


# ---------------------------------------------------------------------------
# Response rewriting
# ---------------------------------------------------------------------------
def _rewrite_instance(block: str, records: dict[str, dict]) -> str:
    instance_id = _tag(block, "DBInstanceIdentifier")
    record = records.get(instance_id or "")
    if record is not None:
        address = instances.endpoint_address(instance_id or "", record["region"])
        endpoint = (
            f"<Endpoint><Address>{address}</Address>"
            f"<Port>{record['port']}</Port></Endpoint>"
        )
        if _ENDPOINT.search(block):
            block = _ENDPOINT.sub(endpoint, block, count=1)
        else:
            block += endpoint
        status = f"<DBInstanceStatus>{record['status']}</DBInstanceStatus>"
        block = _STATUS.sub(status, block, count=1)
        if record.get("engine_version"):
            version = f"<EngineVersion>{record['engine_version']}</EngineVersion>"
            block = _VERSION.sub(version, block, count=1)
        if record.get("promoted"):
            block = _SOURCE.sub("", block)

    # moto keeps a promoted replica in its source's list; RDS drops it
    def prune(m: re.Match) -> str:
        kept = [
            r.group(0)
            for r in _REPLICA.finditer(m.group(1))
            if not records.get(r.group(2), {}).get("promoted")
        ]
        inner = "".join(kept)
        return f"<ReadReplicaDBInstanceIdentifiers>{inner}</ReadReplicaDBInstanceIdentifiers>"

    return _REPLICAS.sub(prune, block)


def rewrite(xml: str, records: dict[str, dict]) -> str:
    """Point every DBInstance oblako runs at its real endpoint and status."""
    if not records or "<DBInstance>" not in xml:
        return xml
    return _INSTANCE.sub(
        lambda m: f"<DBInstance>{_rewrite_instance(m.group(1), records)}</DBInstance>",
        xml,
    )


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------
class RdsControlProxy:
    """Forwards RDS calls to moto and runs PostgreSQL instances for them."""

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

    def _boto(self, region: str):
        import boto3

        return boto3.client(
            "rds",
            endpoint_url=self.backend,
            region_name=region,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        )

    def _logical(self, region: str, group: str | None) -> bool:
        """Return True if the parameter group sets ``rds.logical_replication = 1``."""
        if not group or group.startswith("default."):
            return False
        rds = self._boto(region)
        pages = rds.get_paginator("describe_db_parameters").paginate(
            DBParameterGroupName=group
        )
        for page in pages:
            for p in page["Parameters"]:
                if p["ParameterName"] == "rds.logical_replication":
                    return str(p.get("ParameterValue", "0")) == "1"
        return False

    def _backup_retention(self, region: str, instance_id: str) -> int:
        found = self._boto(region).describe_db_instances(
            DBInstanceIdentifier=instance_id
        )
        return found["DBInstances"][0].get("BackupRetentionPeriod", 0)

    def _parameter_group(self, region: str, instance_id: str) -> str | None:
        rds = self._boto(region)
        found = rds.describe_db_instances(DBInstanceIdentifier=instance_id)
        groups = found["DBInstances"][0].get("DBParameterGroups", [])
        return groups[0]["DBParameterGroupName"] if groups else None

    def _precheck(
        self, action: str, form: dict[str, str], region: str
    ) -> Response | None:
        """Refuse what RDS refuses: busy instances, replicas of unbacked-up sources."""
        if action == "CreateDBInstanceReadReplica":
            source_id = form.get("SourceDBInstanceIdentifier", "")
            source = instances.get(source_id)
            if source is not None and source["status"] != "available":
                return error_response(
                    "InvalidDBInstanceState",
                    f"Source instance is {source['status']}, not available.",
                )
            if source is not None and self._backup_retention(region, source_id) == 0:
                return error_response(
                    "InvalidDBInstanceState",
                    "Automated backups are not enabled for this database instance. "
                    "To enable automated backups, use ModifyDBInstance to set the "
                    "backup retention period to a non-zero value.",
                )
        if action in ("RebootDBInstance", "PromoteReadReplica"):
            record = instances.get(form.get("DBInstanceIdentifier", ""))
            if record is not None and record["status"] != "available":
                return error_response(
                    "InvalidDBInstanceState",
                    f"Instance is {record['status']}, not available.",
                )
        return None

    def _apply(self, action: str, form: dict[str, str], region: str) -> None:
        """Do to the real instances what moto just accepted."""
        instance_id = form.get("DBInstanceIdentifier", "")
        if action == "CreateDBInstance":
            if form.get("Engine") != "postgres" or form.get("DBClusterIdentifier"):
                return
            instances.create_primary(
                instance_id,
                user=form.get("MasterUsername", "postgres"),
                password=form.get("MasterUserPassword", ""),
                database=form.get("DBName") or "postgres",
                region=region,
                logical=self._logical(region, form.get("DBParameterGroupName")),
            )
        elif action == "CreateDBInstanceReadReplica":
            source_id = form.get("SourceDBInstanceIdentifier", "")
            if instances.get(source_id) is not None:
                instances.create_replica(instance_id, source_id)
        elif action == "PromoteReadReplica":
            instances.promote(instance_id)
        elif action == "RebootDBInstance":
            if instances.get(instance_id) is not None:
                group = self._parameter_group(region, instance_id)
                instances.reboot(instance_id, self._logical(region, group))
        elif action == "DeleteDBInstance":
            if instances.get(instance_id) is not None:
                instances.delete(instance_id)

    async def handle(self, request: Request) -> Response:
        """Forward one RDS call to moto, act on it, and rewrite its endpoints."""
        body = await request.body()
        form = {k: v[0] for k, v in parse_qs(body.decode(errors="replace")).items()}
        action = form.get("Action", "")
        region = region_of(request.headers)
        refused = await run_in_threadpool(self._precheck, action, form, region)
        if refused is not None:
            return refused
        upstream = await self._forward(request, body)
        if upstream.status_code == 200:
            try:
                await run_in_threadpool(self._apply, action, form, region)
            except Exception as e:
                return error_response("InternalFailure", str(e), status=500)
        content = upstream.content
        if upstream.status_code == 200 and b"<DBInstance>" in content:
            content = rewrite(content.decode(), instances.load()).encode()
        return Response(
            content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/xml"),
        )


def create_app(backend_url: str | None = None) -> Starlette:
    """Build the proxy's ASGI app."""
    proxy = RdsControlProxy(backend_url)
    return Starlette(
        routes=[Route("/{path:path}", proxy.handle, methods=["GET", "POST"])]
    )


app = create_app()
