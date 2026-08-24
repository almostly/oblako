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
        self.wfile.write(json.dumps({"Id": "stmt-1"}).encode())

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
