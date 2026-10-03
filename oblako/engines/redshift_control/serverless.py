"""Redshift Serverless on oblako's Redshift engine.

AWS runs a Serverless **namespace** (the databases, users and IAM roles) and a
**workgroup** (the compute and its endpoint) as two resources. oblako keeps both
as records and serves every workgroup from the shared Redshift engine on
``localhost:5439``, so drivers connect to a workgroup's endpoint as on AWS:

* ``CreateNamespace`` creates its admin user (with the given password) and its
  database (``dev`` by default) in the engine; ``DeleteNamespace`` drops what
  it created, refusing while a workgroup still uses the namespace.
* ``CreateWorkgroup`` reports the engine's address and port as its endpoint.
* ``Get*``, ``List*`` and ``Delete*`` for both, with AWS's error codes.

State lives in ``~/.oblako/redshift/serverless.json``. The API is the JSON
protocol behind ``X-Amz-Target: RedshiftServerless.<Operation>``, served on the
Redshift control port next to the provisioned-cluster API.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from oblako import config, ports

STATE = Path.home() / ".oblako" / "redshift" / "serverless.json"
DEFAULT_DATABASE = "dev"
_NAME = re.compile(r"^[a-z0-9-]{3,64}$")
_lock = threading.RLock()


class ServerlessError(Exception):
    """An API error, raised with AWS's exception name as its code."""

    def __init__(self, code: str, message: str):
        """Keep AWS's exception name as ``code`` and its text as ``message``."""
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load() -> dict[str, dict]:
    """Return ``{"namespaces": {...}, "workgroups": {...}}`` keyed by name."""
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"namespaces": {}, "workgroups": {}}


def _save(state: dict[str, dict]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)


def get_workgroup_record(name: str) -> dict | None:
    """Return a workgroup's record, or None (for the Data API)."""
    return load()["workgroups"].get(name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _arn(kind: str, resource_id: str) -> str:
    return (
        f"arn:aws:redshift-serverless:{config.region()}:{config.account_id()}"
        f":{kind}/{resource_id}"
    )


def _check_name(kind: str, name: str | None) -> str:
    if not name or not _NAME.match(name):
        raise ServerlessError(
            "ValidationException",
            f"{kind} name must be 3 to 64 lowercase letters, digits or hyphens",
        )
    return name


# ---------------------------------------------------------------------------
# The engine: a namespace's admin user and database
# ---------------------------------------------------------------------------
def _engine():
    import psycopg

    from oblako.services import RedshiftService

    svc = RedshiftService()
    return psycopg.connect(
        host="localhost",
        port=svc.host_port,
        user=svc.user,
        password=svc.password,
        dbname=svc.database,
        autocommit=True,
    )


def _create_in_engine(user: str | None, password: str | None, database: str) -> dict:
    """Create the admin user and the database; report which ones were new."""
    from psycopg import sql

    created = {"user": False, "database": False}
    with _engine() as conn:
        if user:
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)
            ).fetchone()
            if not exists:
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN SUPERUSER PASSWORD {}").format(
                        sql.Identifier(user), sql.Literal(password or "")
                    )
                )
                created["user"] = True
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if not exists:
            statement = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            if user:  # otherwise the engine's own user owns it
                statement += sql.SQL(" OWNER {}").format(sql.Identifier(user))
            conn.execute(statement)
            created["database"] = True
    return created


def _drop_from_engine(record: dict) -> None:
    """Drop the user and database the namespace created, and nothing else."""
    from psycopg import sql

    created = record.get("created", {})
    with _engine() as conn:
        if created.get("database"):
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(record["dbName"])
                )
            )
        if created.get("user"):
            role = sql.Identifier(record["adminUsername"])
            conn.execute(sql.SQL("DROP OWNED BY {}").format(role))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(role))


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------
def _namespace_view(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in ("created", "tags")}


def _workgroup_view(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "tags"}


def create_namespace(req: dict) -> dict:
    """Create a namespace, with its admin user and database in the engine."""
    name = _check_name("Namespace", req.get("namespaceName"))
    with _lock:
        state = load()
        if name in state["namespaces"]:
            raise ServerlessError(
                "ConflictException", f"Namespace {name} already exists"
            )
        user = req.get("adminUsername")
        if user and not req.get("adminUserPassword"):
            raise ServerlessError(
                "ValidationException", "adminUserPassword is required"
            )
        database = req.get("dbName") or DEFAULT_DATABASE
        namespace_id = str(uuid.uuid4())
        created = _create_in_engine(user, req.get("adminUserPassword"), database)
        record = {
            "namespaceName": name,
            "namespaceId": namespace_id,
            "namespaceArn": _arn("namespace", namespace_id),
            "adminUsername": user,
            "dbName": database,
            "iamRoles": req.get("iamRoles", []),
            "defaultIamRoleArn": req.get("defaultIamRoleArn"),
            "logExports": req.get("logExports", []),
            "status": "AVAILABLE",
            "creationDate": _now(),
            "created": created,
            "tags": {t["key"]: t["value"] for t in req.get("tags", [])},
        }
        if not user:
            del record["adminUsername"]
        if not record["defaultIamRoleArn"]:
            del record["defaultIamRoleArn"]
        state["namespaces"][name] = record
        _save(state)
    return {"namespace": _namespace_view(record)}


def _namespace(state: dict, name: str | None) -> dict:
    record = state["namespaces"].get(name or "")
    if record is None:
        raise ServerlessError(
            "ResourceNotFoundException", f"Namespace {name} not found"
        )
    return record


def get_namespace(req: dict) -> dict:
    """Return one namespace."""
    return {"namespace": _namespace_view(_namespace(load(), req.get("namespaceName")))}


def list_namespaces(req: dict) -> dict:
    """Return every namespace."""
    return {"namespaces": [_namespace_view(r) for r in load()["namespaces"].values()]}


def delete_namespace(req: dict) -> dict:
    """Delete a namespace no workgroup uses, with the user and database it created."""
    name = req.get("namespaceName")
    with _lock:
        state = load()
        record = _namespace(state, name)
        users = [
            w["workgroupName"]
            for w in state["workgroups"].values()
            if w["namespaceName"] == name
        ]
        if users:
            raise ServerlessError(
                "ConflictException",
                f"Namespace {name} is used by workgroup {users[0]}; delete it first",
            )
        _drop_from_engine(record)
        del state["namespaces"][name]
        _save(state)
    return {"namespace": {**_namespace_view(record), "status": "DELETING"}}


# ---------------------------------------------------------------------------
# Workgroups
# ---------------------------------------------------------------------------
def create_workgroup(req: dict) -> dict:
    """Create a workgroup in an existing namespace; its endpoint is the engine."""
    name = _check_name("Workgroup", req.get("workgroupName"))
    with _lock:
        state = load()
        if name in state["workgroups"]:
            raise ServerlessError(
                "ConflictException", f"Workgroup {name} already exists"
            )
        _namespace(state, req.get("namespaceName"))
        workgroup_id = str(uuid.uuid4())
        # every workgroup is the shared engine, so its endpoint is the engine's
        record = {
            "workgroupName": name,
            "workgroupId": workgroup_id,
            "workgroupArn": _arn("workgroup", workgroup_id),
            "namespaceName": req["namespaceName"],
            "baseCapacity": int(req.get("baseCapacity", 128)),
            "enhancedVpcRouting": bool(req.get("enhancedVpcRouting", False)),
            "publiclyAccessible": bool(req.get("publiclyAccessible", False)),
            "securityGroupIds": req.get("securityGroupIds", []),
            "subnetIds": req.get("subnetIds", []),
            "port": ports.REDSHIFT_PG,
            "endpoint": {"address": "localhost", "port": ports.REDSHIFT_PG},
            "status": "AVAILABLE",
            "creationDate": _now(),
            "tags": {t["key"]: t["value"] for t in req.get("tags", [])},
        }
        if "maxCapacity" in req:
            record["maxCapacity"] = int(req["maxCapacity"])
        state["workgroups"][name] = record
        _save(state)
    return {"workgroup": _workgroup_view(record)}


def _workgroup(state: dict, name: str | None) -> dict:
    record = state["workgroups"].get(name or "")
    if record is None:
        raise ServerlessError(
            "ResourceNotFoundException", f"Workgroup {name} not found"
        )
    return record


def get_workgroup(req: dict) -> dict:
    """Return one workgroup."""
    return {"workgroup": _workgroup_view(_workgroup(load(), req.get("workgroupName")))}


def list_workgroups(req: dict) -> dict:
    """Return every workgroup."""
    return {"workgroups": [_workgroup_view(r) for r in load()["workgroups"].values()]}


def delete_workgroup(req: dict) -> dict:
    """Delete a workgroup."""
    name = req.get("workgroupName")
    with _lock:
        state = load()
        record = _workgroup(state, name)
        del state["workgroups"][name]
        _save(state)
    return {"workgroup": {**_workgroup_view(record), "status": "DELETING"}}


# ---------------------------------------------------------------------------
# Tags (on namespaces and workgroups, by ARN)
# ---------------------------------------------------------------------------
def _by_arn(state: dict, arn: str) -> dict:
    for kind in ("namespaces", "workgroups"):
        for record in state[kind].values():
            if arn in (record.get("namespaceArn"), record.get("workgroupArn")):
                return record
    raise ServerlessError("ResourceNotFoundException", f"Resource {arn} not found")


def tag_resource(req: dict) -> dict:
    """Add or replace tags on a namespace or workgroup."""
    with _lock:
        state = load()
        record = _by_arn(state, req.get("resourceArn", ""))
        tags = record.setdefault("tags", {})
        tags.update({t["key"]: t["value"] for t in req.get("tags", [])})
        _save(state)
    return {}


def untag_resource(req: dict) -> dict:
    """Remove tags from a namespace or workgroup."""
    with _lock:
        state = load()
        record = _by_arn(state, req.get("resourceArn", ""))
        for key in req.get("tagKeys", []):
            record.get("tags", {}).pop(key, None)
        _save(state)
    return {}


def list_tags_for_resource(req: dict) -> dict:
    """Return a namespace's or workgroup's tags."""
    record = _by_arn(load(), req.get("resourceArn", ""))
    tags = record.get("tags", {})
    return {"tags": [{"key": k, "value": v} for k, v in tags.items()]}


OPERATIONS = {
    "CreateNamespace": create_namespace,
    "GetNamespace": get_namespace,
    "ListNamespaces": list_namespaces,
    "DeleteNamespace": delete_namespace,
    "CreateWorkgroup": create_workgroup,
    "GetWorkgroup": get_workgroup,
    "ListWorkgroups": list_workgroups,
    "DeleteWorkgroup": delete_workgroup,
    "TagResource": tag_resource,
    "UntagResource": untag_resource,
    "ListTagsForResource": list_tags_for_resource,
}


def call(operation: str, req: dict) -> dict:
    """Run one operation; raise ServerlessError for AWS-shaped errors."""
    handler = OPERATIONS.get(operation)
    if handler is None:
        raise ServerlessError(
            "ValidationException",
            f"oblako's Redshift Serverless does not support {operation}",
        )
    return handler(req)
