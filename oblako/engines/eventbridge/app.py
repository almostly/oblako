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


_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_STARTED = False


def _moto_url() -> str:
    import os

    return os.environ.get("OBLAKO_MOTO_ENDPOINT") or f"http://localhost:{ports.MOTO}"


class EventBridgeProxy:
    """Forwards EventBridge ops to moto and fires rule targets on PutEvents."""

    def __init__(self, backend_url: str | None = None):
        """Bind to the moto endpoint and start the scheduled-rule firing loop."""
        self.backend = (backend_url or _moto_url()).rstrip("/")
        self._last_fired: dict[str, float] = {}
        # one scheduler per process: the module builds an app at import and the
        # engine builds another, and two loops fired every scheduled rule twice
        global _SCHEDULER_STARTED
        with _SCHEDULER_LOCK:
            if not _SCHEDULER_STARTED:
                _SCHEDULER_STARTED = True
                threading.Thread(target=self._schedule_loop, daemon=True).start()

    def _schedule_loop(self) -> None:
        """Fire ScheduleExpression rules (rate(...) and cron(...)), checked every 1s."""
        import boto3

        while True:
            time.sleep(1.0)
            try:
                events = boto3.client("events", endpoint_url=self.backend, **_creds())
                self._fire_scheduled(events, time.monotonic())
            except Exception:
                pass  # moto may be momentarily unreachable; retry next tick

    def _fire_scheduled(
        self, events, now: float, utc: datetime.datetime | None = None
    ) -> list[str]:
        """Fire every due scheduled rule once; return the names fired (one pass).

        ``rate(...)`` rules fire one interval after they are first seen, then on
        that cadence; ``cron(...)`` rules fire once in each UTC minute they match.
        """
        utc = utc or datetime.datetime.now(datetime.timezone.utc)
        minute = utc.replace(second=0, microsecond=0).timestamp()
        fired = []
        for rule in events.list_rules().get("Rules", []):
            expr = rule.get("ScheduleExpression") or ""
            if rule.get("State") != "ENABLED":
                continue
            if expr.startswith("cron("):
                if (
                    not cron_matches(expr, utc)
                    or self._last_fired.get(rule["Name"]) == minute
                ):
                    continue
                self._last_fired[rule["Name"]] = minute
                due = True
            else:
                interval = _rate_seconds(expr) if expr else None
                if interval is None:
                    continue
                last = self._last_fired.get(rule["Name"])
                if last is None:
                    self._last_fired[rule["Name"]] = (
                        now  # first fire after one interval
                    )
                    continue
                due = now - last >= interval
                if due:
                    self._last_fired[rule["Name"]] = now
            if due:
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
    except Exception as e:  # delivery is best effort, as EventBridge retries
        print(f"oblako eventbridge: delivering to {arn} failed: {e!r}", flush=True)


def _deliver_redshift(params: dict, target_arn: str) -> None:
    """Run an EventBridge Redshift Data target through oblako's redshift-data API.

    The scheduled-query path: a rule (query editor v2 names them ``QS2-...``)
    whose target is a cluster or a Serverless workgroup, with
    RedshiftDataParameters. ``Sql`` runs as ExecuteStatement, ``Sqls`` as one
    BatchExecuteStatement (a single transaction), with ``DbUser`` or
    ``SecretManagerArn``. ``WithEvent`` puts a "Redshift Data Statement Status
    Change" event on the default bus when the statement finishes.
    """
    import os

    import boto3

    endpoint = os.environ.get("AWS_ENDPOINT_URL_REDSHIFT_DATA") or (
        f"http://localhost:{ports.REDSHIFT_DATA}"
    )
    rd = boto3.client("redshift-data", endpoint_url=endpoint, **_creds())
    kwargs: dict = {"Database": params.get("Database")}
    kind, name = _target_from_arn(target_arn)
    if kind == "workgroup":
        kwargs["WorkgroupName"] = name
    elif kind == "cluster":
        kwargs["ClusterIdentifier"] = name
    if params.get("DbUser"):
        kwargs["DbUser"] = params["DbUser"]
    if params.get("SecretManagerArn"):
        kwargs["SecretArn"] = params["SecretManagerArn"]
    if params.get("StatementName"):
        kwargs["StatementName"] = params["StatementName"]
    if params.get("WithEvent"):
        kwargs["WithEvent"] = True
    if params.get("Sqls"):
        statement = rd.batch_execute_statement(Sqls=params["Sqls"], **kwargs)
    else:
        statement = rd.execute_statement(Sql=params.get("Sql", ""), **kwargs)
    if params.get("WithEvent"):
        threading.Thread(
            target=_status_change_event,
            args=(rd, statement["Id"], params, "Sqls" in params),
            daemon=True,
        ).start()


def _status_change_event(rd, statement_id: str, params: dict, batch: bool) -> None:
    """Wait for a statement, then put Redshift Data's status-change event."""
    for _ in range(3600):
        desc = rd.describe_statement(Id=statement_id)
        if desc["Status"] in ("FINISHED", "FAILED", "ABORTED"):
            break
        time.sleep(1)
    else:
        return
    detail = {
        "principal": params.get("DbUser") or params.get("SecretManagerArn") or "",
        "statementName": params.get("StatementName") or "",
        "statementId": statement_id,
        "redshiftQueryId": desc.get("RedshiftQueryId", 0),
        "state": desc["Status"],
        "type": "BatchExecuteStatement" if batch else "ExecuteStatement",
    }
    if desc["Status"] == "FINISHED" and desc.get("HasResultSet"):
        detail["rows"] = desc.get("ResultRows", 0)
    _emit_service_event(
        "aws.redshift-data", "Redshift Data Statement Status Change", detail
    )


def _emit_service_event(source: str, detail_type: str, detail: dict) -> None:
    """Put an AWS service's event on the default bus, as the service itself does.

    ``aws.*`` sources are reserved: PutEvents refuses them, and AWS services emit
    their events inside EventBridge. So the proxy matches the bus's rules itself
    and delivers to every target (moto, which never sees the event, delivers none).
    """
    import boto3

    moto = _moto_url()
    events = boto3.client("events", endpoint_url=moto, **_creds())
    event = {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": detail_type,
        "source": source,
        "account": _ACCOUNT,
        "time": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "region": _REGION,
        "resources": [],
        "detail": detail,
    }
    for pattern, targets in EventBridgeProxy._rules_with_targets(events):
        if _matches(pattern, event):
            for target in targets:
                _deliver(target, event, moto)


def _target_from_arn(arn: str) -> tuple[str, str] | tuple[None, None]:
    """Return ("cluster" | "workgroup", name) for a Redshift target ARN."""
    if ":cluster:" in arn:
        return "cluster", arn.split(":cluster:")[-1]
    if ":workgroup/" in arn:
        return "workgroup", arn.split(":workgroup/")[-1]
    return None, None


# ---------------------------------------------------------------------------------
# cron(...) schedules, as EventBridge reads them (UTC)
# ---------------------------------------------------------------------------------
_MONTHS: dict[str, int] = {
    m: i
    for i, m in enumerate(
        "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), start=1
    )
}
_DAYS: dict[str, int] = {
    d: i for i, d in enumerate("SUN MON TUE WED THU FRI SAT".split(), start=1)
}
_NUMBERS: dict[str, int] = {}  # fields with no names


def _field_values(field: str, low: int, high: int, names: dict[str, int]) -> set[int]:
    """Expand one cron field (``*``, lists, ranges, steps, names) to its values."""
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
        if part in ("*", "?"):
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = (
                names.get(a.upper()) or int(a),
                names.get(b.upper()) or int(b),
            )
        else:
            start = names.get(part.upper()) or int(part)
            end = high if step > 1 else start
        values.update(range(start, end + 1, step))
    return values


def cron_matches(expression: str, when: datetime.datetime) -> bool:
    """Whether ``cron(min hour day-of-month month day-of-week year)`` matches ``when``.

    EventBridge's six fields: day-of-week counts SUN=1 to SAT=7, ``?`` leaves a day
    field open, ``L`` is the last day of the month (or ``5L``, its last Thursday),
    and ``3#2`` is the second Tuesday. ``W`` isn't supported.
    """
    import calendar

    fields = expression.strip()[len("cron(") : -1].split()
    if len(fields) != 6:
        return False
    minute, hour, dom, month, dow, year = fields
    if when.minute not in _field_values(minute, 0, 59, _NUMBERS):
        return False
    if when.hour not in _field_values(hour, 0, 23, _NUMBERS):
        return False
    if when.month not in _field_values(month, 1, 12, _MONTHS):
        return False
    if year not in ("*", "?") and when.year not in _field_values(
        year, 1970, 2199, _NUMBERS
    ):
        return False
    last_day = calendar.monthrange(when.year, when.month)[1]
    weekday = (when.isoweekday() % 7) + 1  # SUN=1 ... SAT=7
    if dom == "L":
        dom_ok = when.day == last_day
    else:
        dom_ok = dom == "?" or when.day in _field_values(dom, 1, 31, _NUMBERS)
    if dow.endswith("L") and len(dow) > 1:
        target = _DAYS.get(dow[:-1].upper()) or int(dow[:-1])
        dow_ok = weekday == target and when.day + 7 > last_day
    elif "#" in dow:
        day_text, nth = dow.split("#", 1)
        target = _DAYS.get(day_text.upper()) or int(day_text)
        dow_ok = weekday == target and (when.day - 1) // 7 + 1 == int(nth)
    else:
        dow_ok = dow == "?" or weekday in _field_values(dow, 1, 7, _DAYS)
    return dom_ok and dow_ok


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
