"""EventBridge proxy over moto that fills its two target-delivery gaps.

moto serves the EventBridge control plane and, on PutEvents, already delivers to
SQS / SNS / Lambda targets itself. It does NOT deliver to **Redshift Data** targets,
and it never fires **scheduled** rules (ScheduleExpression). This proxy forwards
every op to moto and adds exactly those two: on PutEvents it runs the matched
rules' Redshift Data targets (the EventBridge -> Redshift Data API path), and a
background loop fires scheduled rules on their cadence (delivering to all of the
rule's targets, since moto fires none). Event-pattern matching supports value
lists, nested detail objects, and the common content filters.
"""

from __future__ import annotations

import datetime
import json
import re
import threading
import time
import uuid

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from oblako import ports

_TARGET_PREFIX = "AWSEvents"
_JSON = "application/x-amz-json-1.1"
_REGION = "us-east-1"
_ACCOUNT = "123456789012"


def _moto_url() -> str:
    import os

    return os.environ.get("OBLAKO_MOTO_ENDPOINT") or f"http://localhost:{ports.MOTO}"


class EventBridgeProxy:
    """Forwards EventBridge ops to moto and fires rule targets on PutEvents."""

    def __init__(self, backend_url: str | None = None):
        """Bind to the moto endpoint and start the scheduled-rule firing loop."""
        self.backend = (backend_url or _moto_url()).rstrip("/")
        self._last_fired: dict[str, float] = {}
        threading.Thread(target=self._schedule_loop, daemon=True).start()

    def _schedule_loop(self) -> None:
        """Fire ScheduleExpression rules (rate(...)) on their cadence, every 1s."""
        import boto3

        while True:
            time.sleep(1.0)
            try:
                events = boto3.client("events", endpoint_url=self.backend, **_creds())
                self._fire_scheduled(events, time.monotonic())
            except Exception:
                pass  # moto may be momentarily unreachable; retry next tick

    def _fire_scheduled(self, events, now: float) -> list[str]:
        """Fire every due scheduled rule once; return the names fired (one pass)."""
        fired = []
        for rule in events.list_rules().get("Rules", []):
            expr = rule.get("ScheduleExpression")
            interval = _rate_seconds(expr) if expr else None
            if interval is None or rule.get("State") != "ENABLED":
                continue
            last = self._last_fired.get(rule["Name"])
            if last is None:
                self._last_fired[rule["Name"]] = now  # first fire after one interval
                continue
            if now - last >= interval:
                self._last_fired[rule["Name"]] = now
                event = _scheduled_event(rule)
                for target in events.list_targets_by_rule(Rule=rule["Name"]).get(
                    "Targets", []
                ):
                    _deliver(target, event, self.backend)
                fired.append(rule["Name"])
        return fired

    async def handle(self, request: Request) -> Response:
        """Forward the op to moto; on PutEvents also deliver to matched targets."""
        op = request.headers.get("x-amz-target", "").split(".")[-1]
        body = await request.body()
        auth = {
            name: request.headers[name]
            for name in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token")
            if name in request.headers
        }
        status, raw = await self._forward(op, body, auth)
        if op == "PutEvents":
            try:
                entries = json.loads(body).get("Entries", [])
            except json.JSONDecodeError:
                entries = []
            threading.Thread(
                target=self._deliver_all, args=(entries,), daemon=True
            ).start()
        return Response(raw, status_code=status, media_type=_JSON)

    async def _forward(self, op: str, body: bytes, auth: dict) -> tuple[int, bytes]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.backend,
                content=body,
                headers={
                    "X-Amz-Target": f"{_TARGET_PREFIX}.{op}",
                    "Content-Type": _JSON,
                    **auth,
                },
            )
        return resp.status_code, resp.content

    def _deliver_all(self, entries: list[dict]) -> None:
        """Deliver matched events to Redshift Data targets (moto does SQS/SNS/Lambda)."""
        import boto3

        events = boto3.client("events", endpoint_url=self.backend, **_creds())
        rules = self._rules_with_targets(events)
        for entry in entries:
            event = _event_envelope(entry)
            for pattern, targets in rules:
                if _matches(pattern, event):
                    for target in targets:
                        if target.get("RedshiftDataParameters"):
                            _deliver(target, event, self.backend)

    @staticmethod
    def _rules_with_targets(events) -> list[tuple[dict, list[dict]]]:
        out = []
        for rule in events.list_rules().get("Rules", []):
            if rule.get("State") != "ENABLED" or not rule.get("EventPattern"):
                continue
            try:
                pattern = json.loads(rule["EventPattern"])
            except json.JSONDecodeError:
                continue
            targets = events.list_targets_by_rule(Rule=rule["Name"]).get("Targets", [])
            out.append((pattern, targets))
        return out


def _creds() -> dict:
    import os

    return {
        "region_name": os.environ.get("AWS_DEFAULT_REGION", _REGION),
        "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    }


def _event_envelope(entry: dict) -> dict:
    """Build the EventBridge event envelope delivered to targets."""
    try:
        detail = json.loads(entry.get("Detail", "{}"))
    except json.JSONDecodeError:
        detail = {}
    return {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": entry.get("DetailType"),
        "source": entry.get("Source"),
        "account": _ACCOUNT,
        "time": entry.get("Time")
        or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "region": _REGION,
        "resources": entry.get("Resources", []),
        "detail": detail,
    }


def _matches(pattern: dict, event: dict) -> bool:
    """Return True if the event matches the EventBridge pattern."""
    for key, expected in pattern.items():
        actual = event.get(key)
        if isinstance(expected, dict):
            if not isinstance(actual, dict) or not _matches(expected, actual):
                return False
        elif isinstance(expected, list):
            if not _match_value(actual, expected):
                return False
        elif actual != expected:
            return False
    return True


def _match_value(actual, allowed: list) -> bool:
    """Match a value against a pattern list (plain values or content filters)."""
    for candidate in allowed:
        if isinstance(candidate, dict):
            if _content_filter(actual, candidate):
                return True
        elif candidate == actual:
            return True
    return False


def _content_filter(actual, spec: dict) -> bool:
    """Evaluate one EventBridge content filter (prefix/suffix/exists/anything-but)."""
    if "exists" in spec:
        return spec["exists"] == (actual is not None)
    if actual is None:
        return False
    if "prefix" in spec:
        return isinstance(actual, str) and actual.startswith(spec["prefix"])
    if "suffix" in spec:
        return isinstance(actual, str) and actual.endswith(spec["suffix"])
    if "anything-but" in spec:
        excluded = spec["anything-but"]
        excluded = excluded if isinstance(excluded, list) else [excluded]
        return actual not in excluded
    if "equals-ignore-case" in spec:
        return (
            isinstance(actual, str)
            and actual.lower() == str(spec["equals-ignore-case"]).lower()
        )
    return False


def _deliver(target: dict, event: dict, moto_url: str) -> None:
    """Deliver one matched event to a target (SQS / SNS / Lambda / Redshift Data)."""
    import boto3

    arn = target.get("Arn", "")
    payload = target.get("Input") or json.dumps(event)
    try:
        if target.get("RedshiftDataParameters"):
            _deliver_redshift(target["RedshiftDataParameters"], arn)
        elif ":sqs:" in arn:
            sqs = boto3.client("sqs", endpoint_url=moto_url, **_creds())
            url = sqs.get_queue_url(QueueName=arn.split(":")[-1])["QueueUrl"]
            sqs.send_message(QueueUrl=url, MessageBody=payload)
        elif ":sns:" in arn:
            sns = boto3.client("sns", endpoint_url=moto_url, **_creds())
            sns.publish(TopicArn=arn, Message=payload)
        elif ":lambda:" in arn:
            lam = boto3.client("lambda", endpoint_url=moto_url, **_creds())
            lam.invoke(
                FunctionName=arn.split(":function:")[-1], Payload=payload.encode()
            )
    except Exception:
        pass  # best-effort delivery, like a dead-letter would swallow


def _deliver_redshift(params: dict, cluster_arn: str) -> None:
    """Run an EventBridge Redshift Data target: ExecuteStatement via redshift-data.

    This is the EventBridge -> Redshift Data API scheduled-query path: a rule
    (typically ScheduleExpression) whose target carries RedshiftDataParameters
    runs its SQL against oblako's local Redshift through the redshift-data engine.
    """
    import os

    import boto3

    endpoint = os.environ.get("AWS_ENDPOINT_URL_REDSHIFT_DATA") or (
        f"http://localhost:{ports.REDSHIFT_DATA}"
    )
    rd = boto3.client("redshift-data", endpoint_url=endpoint, **_creds())
    sqls = params.get("Sqls") or ([params["Sql"]] if params.get("Sql") else [])
    for sql in sqls:
        kwargs = {"Database": params.get("Database"), "Sql": sql}
        cluster = _cluster_from_arn(cluster_arn)
        if cluster:
            kwargs["ClusterIdentifier"] = cluster
        if params.get("DbUser"):
            kwargs["DbUser"] = params["DbUser"]
        if params.get("StatementName"):
            kwargs["StatementName"] = params["StatementName"]
        rd.execute_statement(**kwargs)


def _cluster_from_arn(arn: str) -> str | None:
    """Extract the cluster identifier from a Redshift (Serverless) target ARN."""
    if ":cluster:" in arn:
        return arn.split(":cluster:")[-1]
    if "workgroup/" in arn:
        return arn.split("workgroup/")[-1]
    return None


def _rate_seconds(expression: str) -> int | None:
    """Parse ``rate(N unit)`` into seconds (locally we honor a seconds unit too)."""
    match = re.fullmatch(
        r"rate\((\d+)\s+(second|minute|hour|day)s?\)", expression.strip()
    )
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    return value * {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[unit]


def _scheduled_event(rule: dict) -> dict:
    """Build the synthetic 'Scheduled Event' EventBridge delivers for a schedule."""
    return {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": "Scheduled Event",
        "source": "aws.events",
        "account": _ACCOUNT,
        "time": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "region": _REGION,
        "resources": [rule.get("Arn", "")],
        "detail": {},
    }


def create_app(backend_url: str | None = None) -> Starlette:
    """Create the Starlette proxy app (forwards to moto's EventBridge)."""
    proxy = EventBridgeProxy(backend_url)
    return Starlette(routes=[Route("/", proxy.handle, methods=["POST"])])


app = create_app()
