"""EventBridge target delivery: PutEvents fires targets; scheduled rules too.

moto stores rules/targets but never delivers them; oblako's proxy does. These
tests drive unmodified boto3 ``events`` at the proxy and assert a matched event
reaches an SQS target, and that a ScheduleExpression rule with a Redshift Data
target runs its SQL via the redshift-data API. Needs moto (events + sqs).
"""

import http.server
import json
import threading
import time
import uuid

import boto3
import pytest

CREDS = dict(
    region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
)


@pytest.fixture(scope="module")
def moto_endpoint():
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker not available")
    from oblako.services import MotoService

    svc = MotoService()
    try:
        svc.start()
    except Exception as err:
        pytest.skip(f"moto unavailable: {err}")
    return svc.endpoint_url


def _client(service, endpoint):
    return boto3.client(service, endpoint_url=endpoint, **CREDS)


def test_put_events_delivers_matched_event_to_sqs(moto_endpoint):
    from oblako.engines.eventbridge import get_client

    events = get_client(backend_url=moto_endpoint)
    sqs = _client("sqs", moto_endpoint)

    # unique names so the test is isolated from moto's persisted state
    suffix = uuid.uuid4().hex[:8]
    queue_url = sqs.create_queue(QueueName=f"orders-q-{suffix}")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]
    rule = f"orders-rule-{suffix}"

    events.put_rule(
        Name=rule,
        EventPattern=json.dumps({"source": ["oblako.orders"]}),
        State="ENABLED",
    )
    events.put_targets(Rule=rule, Targets=[{"Id": "1", "Arn": queue_arn}])

    try:
        events.put_events(
            Entries=[
                {
                    "Source": "oblako.orders",
                    "DetailType": "OrderPlaced",
                    "Detail": json.dumps({"orderId": 42}),
                },
                {  # non-matching source -> must NOT be delivered
                    "Source": "oblako.other",
                    "DetailType": "Noise",
                    "Detail": "{}",
                },
            ]
        )

        messages = []
        for _ in range(20):
            got = sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=1).get(
                "Messages", []
            )
            if got:
                messages = got
                break
        assert messages, "matched event was not delivered to the SQS target"
        event = json.loads(messages[0]["Body"])
        assert event["source"] == "oblako.orders"
        assert event["detail"]["orderId"] == 42
        sqs.delete_message(
            QueueUrl=queue_url, ReceiptHandle=messages[0]["ReceiptHandle"]
        )
        # only the matching event was delivered (its own unique rule/queue)
        more = sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=1).get(
            "Messages", []
        )
        assert not more
    finally:
        events.remove_targets(Rule=rule, Ids=["1"])
        events.delete_rule(Name=rule)


class _Recorder(http.server.BaseHTTPRequestHandler):
    calls: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        _Recorder.calls.append((self.headers.get("X-Amz-Target", ""), body))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-amz-json-1.1")
        self.end_headers()
        # DescribeStatement reports the statement finished (for WithEvent)
        self.wfile.write(
            json.dumps(
                {"Id": "stmt-1", "Status": "FINISHED", "HasResultSet": False}
            ).encode()
        )

    def log_message(self, *_args):
        pass


def test_scheduled_rule_runs_redshift_data(moto_endpoint, monkeypatch):
    from oblako.engines.eventbridge.app import EventBridgeProxy

    # capture what the redshift-data endpoint receives (no real Redshift needed)
    _Recorder.calls = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(
        "AWS_ENDPOINT_URL_REDSHIFT_DATA", f"http://127.0.0.1:{server.server_address[1]}"
    )

    events = _client("events", moto_endpoint)
    rule = f"nightly-agg-{uuid.uuid4().hex[:8]}"
    events.put_rule(
        Name=rule,
        ScheduleExpression="rate(1 minute)",  # EventBridge/moto min interval
        State="ENABLED",
    )
    events.put_targets(
        Rule=rule,
        Targets=[
            {
                "Id": "1",
                "Arn": "arn:aws:redshift:us-east-1:123456789012:cluster:oblako",
                "RedshiftDataParameters": {
                    "Database": "oblako",
                    "Sql": "INSERT INTO daily_totals SELECT current_date, count(*) FROM orders",
                    "DbUser": "admin",
                },
            }
        ],
    )
    try:
        # one scheduler pass with the rule already due runs the Redshift Data target
        proxy = EventBridgeProxy(backend_url=moto_endpoint)
        proxy._last_fired[rule] = time.monotonic() - 100
        assert rule in proxy._fire_scheduled(events, time.monotonic())

        target, body = next(
            (t, b) for t, b in _Recorder.calls if "ExecuteStatement" in t
        )
        assert body["Sql"].startswith("INSERT INTO daily_totals")
        assert body["Database"] == "oblako"
        assert body["ClusterIdentifier"] == "oblako"
    finally:
        events.remove_targets(Rule=rule, Ids=["1"])
        events.delete_rule(Name=rule)
        server.shutdown()


@pytest.mark.parametrize(
    "expression, when, expected",
    [
        ("cron(0 6 * * ? *)", "2026-10-05 06:00", True),
        ("cron(0 6 * * ? *)", "2026-10-05 06:01", False),
        ("cron(0/15 * * * ? *)", "2026-10-05 09:45", True),
        ("cron(0 10 ? * MON-FRI *)", "2026-10-05 10:00", True),  # a Monday
        ("cron(0 10 ? * MON-FRI *)", "2026-10-04 10:00", False),  # a Sunday
        ("cron(0 0 L * ? *)", "2026-10-31 00:00", True),
        ("cron(0 0 ? * 6L *)", "2026-10-30 00:00", True),  # last Friday
        ("cron(0 0 ? * 3#2 *)", "2026-10-13 00:00", True),  # second Tuesday
        ("cron(30 8 1 JAN,JUL ? 2026)", "2026-07-01 08:30", True),
        ("cron(30 8 1 JAN,JUL ? 2027)", "2026-07-01 08:30", False),
    ],
)
def test_cron_expressions(expression, when, expected):
    import datetime

    from oblako.engines.eventbridge.app import cron_matches

    at = datetime.datetime.strptime(when, "%Y-%m-%d %H:%M").replace(
        tzinfo=datetime.timezone.utc
    )
    assert cron_matches(expression, at) is expected


def test_query_editor_schedule_on_a_workgroup(moto_endpoint, monkeypatch):
    """A QS2- cron rule, as query editor v2 creates it: Serverless, a batch, a secret.

    The batch runs as one BatchExecuteStatement on the workgroup, and WithEvent
    puts Redshift Data's status-change event on the bus, where a rule routes it.
    """
    import datetime

    from oblako.engines.eventbridge.app import EventBridgeProxy

    _Recorder.calls = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(
        "AWS_ENDPOINT_URL_REDSHIFT_DATA", f"http://127.0.0.1:{server.server_address[1]}"
    )
    events, sqs = _client("events", moto_endpoint), _client("sqs", moto_endpoint)
    rule, done = f"QS2-refresh-{uuid.uuid4().hex[:8]}", f"done-{uuid.uuid4().hex[:8]}"
    queue = sqs.create_queue(QueueName=done)["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    events.put_rule(Name=rule, ScheduleExpression="cron(0 6 * * ? *)", State="ENABLED")
    events.put_targets(
        Rule=rule,
        Targets=[
            {
                "Id": "1",
                "Arn": "arn:aws:redshift-serverless:us-east-1:123456789012:workgroup/analytics",
                "RedshiftDataParameters": {
                    "Database": "dev",
                    "Sqls": ["DELETE FROM daily", "INSERT INTO daily SELECT 1"],
                    "SecretManagerArn": "arn:aws:secretsmanager:us-east-1:1:secret:rs",
                    "StatementName": "refresh",
                    "WithEvent": True,
                },
            }
        ],
    )
    events.put_rule(
        Name=done,
        EventPattern=json.dumps(
            {
                "source": ["aws.redshift-data"],
                "detail-type": ["Redshift Data Statement Status Change"],
            }
        ),
    )
    events.put_targets(Rule=done, Targets=[{"Id": "1", "Arn": queue_arn}])
    try:
        proxy = EventBridgeProxy(backend_url=moto_endpoint)
        six = datetime.datetime(2026, 10, 5, 6, 0, tzinfo=datetime.timezone.utc)
        assert rule in proxy._fire_scheduled(events, time.monotonic(), utc=six)
        # the same minute doesn't fire twice
        assert rule not in proxy._fire_scheduled(events, time.monotonic(), utc=six)
        _, body = next((t, b) for t, b in _Recorder.calls if "BatchExecute" in t)
        assert body["Sqls"] == ["DELETE FROM daily", "INSERT INTO daily SELECT 1"]
        assert body["WorkgroupName"] == "analytics" and "ClusterIdentifier" not in body
        assert body["SecretArn"].endswith(":secret:rs")
        detail = None
        for _ in range(20):
            msgs = sqs.receive_message(QueueUrl=queue, WaitTimeSeconds=1).get(
                "Messages", []
            )
            if msgs:
                detail = json.loads(msgs[0]["Body"])["detail"]
                break
        assert detail is not None, "no status-change event reached the queue"
        assert detail["state"] == "FINISHED"
        assert detail["type"] == "BatchExecuteStatement"
        assert detail["statementName"] == "refresh"
    finally:
        for name in (rule, done):
            events.remove_targets(Rule=name, Ids=["1"])
            events.delete_rule(Name=name)
        sqs.delete_queue(QueueUrl=queue)
        server.shutdown()
