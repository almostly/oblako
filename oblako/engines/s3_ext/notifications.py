"""S3 event notifications for oblako's S3 extensions engine.

``PutBucketNotificationConfiguration`` is stored; events are delivered the way S3
delivers them, to the Lambda functions, SQS queues and SNS topics in moto, and to
EventBridge when it's enabled for the bucket.

The engine learns about writes from nginx: the :9000 front logs every completed
PUT / POST / DELETE (method, URI, status, ETag, size) as a JSON line to a file
the engine follows (``follow_log``). Only successful writes become events, in
the order they finished, so an upload followed at once by a delete yields both
events. Batch deletes (``DeleteObjects``) carry their keys in the request body,
which isn't logged, so they don't produce events.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

NS = "http://s3.amazonaws.com/doc/2006-03-01/"
# element -> (target kind, ARN element). On the wire S3 calls the Lambda entry
# CloudFunctionConfiguration (what boto3 sends); accept the API name too.
_KINDS = {
    "CloudFunctionConfiguration": ("lambda", "CloudFunction"),
    "LambdaFunctionConfiguration": ("lambda", "CloudFunction"),
    "QueueConfiguration": ("sqs", "Queue"),
    "TopicConfiguration": ("sns", "Topic"),
}


@dataclass
class Target:
    """One notification destination and the events it wants."""

    kind: str  # lambda | sqs | sns
    arn: str
    id: str
    events: list[str] = field(default_factory=list)
    prefix: str = ""
    suffix: str = ""

    def matches(self, event: str, key: str) -> bool:
        """Return True if this target wants ``event`` for ``key``."""
        wanted = any(
            event == e or (e.endswith(":*") and event.startswith(e[:-1]))
            for e in self.events
        )
        return wanted and key.startswith(self.prefix) and key.endswith(self.suffix)


def _strip_ns(elem: ET.Element) -> ET.Element:
    """Strip XML namespaces from ``elem`` and its descendants."""
    for node in elem.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    return elem


def normalize(body: bytes) -> str:
    """Validate a NotificationConfiguration and return it namespaced, as S3 does."""
    root = _strip_ns(ET.fromstring(body or b"<NotificationConfiguration/>"))
    if root.tag != "NotificationConfiguration":
        raise ValueError("expected a NotificationConfiguration document")
    for conf in root:
        if conf.tag in _KINDS:
            if not conf.findtext(_KINDS[conf.tag][1]):
                raise ValueError(f"{conf.tag} needs a {_KINDS[conf.tag][1]} ARN")
            if conf.find("Id") is None:  # S3 assigns one
                ET.SubElement(conf, "Id").text = str(uuid.uuid4())
    root.set("xmlns", NS)
    return ET.tostring(root, encoding="unicode")


def parse(xml: str) -> tuple[list[Target], bool]:
    """Return the targets and whether EventBridge delivery is enabled."""
    root = _strip_ns(ET.fromstring(xml))
    targets = []
    for conf in root:
        if conf.tag not in _KINDS:
            continue
        kind, arn_tag = _KINDS[conf.tag]
        rules = {
            (r.findtext("Name") or "").lower(): r.findtext("Value") or ""
            for r in conf.iter("FilterRule")
        }
        targets.append(
            Target(
                kind=kind,
                arn=conf.findtext(arn_tag) or "",
                id=conf.findtext("Id") or "",
                events=[e.text for e in conf.findall("Event") if e.text],
                prefix=rules.get("prefix", ""),
                suffix=rules.get("suffix", ""),
            )
        )
    return targets, root.find("EventBridgeConfiguration") is not None


def event_name(method: str, query: dict, headers: dict) -> str | None:
    """Map a completed write to an S3 event name (None if it isn't one)."""
    if any(q in query for q in ("tagging", "acl", "retention", "legal-hold")):
        return None
    if method == "PUT":
        if "partNumber" in query or "uploadId" in query:
            return None  # a part, not an object
        if "x-amz-copy-source" in headers:
            return "s3:ObjectCreated:Copy"
        return "s3:ObjectCreated:Put"
    if method == "POST" and "uploadId" in query:
        return "s3:ObjectCreated:CompleteMultipartUpload"
    if method == "POST" and not query:
        return "s3:ObjectCreated:Post"  # browser-form upload
    if method == "DELETE" and "uploadId" not in query:
        return "s3:ObjectRemoved:Delete"
    return None


def record(event: str, bucket: str, key: str, head: dict, config_id: str) -> dict:
    """Build an S3 event notification record."""
    now = datetime.now(timezone.utc)
    obj: dict = {
        "key": urllib.parse.quote_plus(key, safe="/"),
        "sequencer": f"{time.time_ns():X}",
    }
    if head:
        obj["size"] = int(head.get("content-length", 0))
        obj["eTag"] = head.get("etag", "").strip('"')
    return {
        "eventVersion": "2.1",
        "eventSource": "aws:s3",
        "awsRegion": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        "eventTime": now.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{now.microsecond // 1000:03d}Z",
        "eventName": event.removeprefix("s3:"),
        "userIdentity": {"principalId": "AWS:oblako"},
        "requestParameters": {"sourceIPAddress": "127.0.0.1"},
        "responseElements": {
            "x-amz-request-id": uuid.uuid4().hex[:16].upper(),
            "x-amz-id-2": uuid.uuid4().hex,
        },
        "s3": {
            "s3SchemaVersion": "1.0",
            "configurationId": config_id,
            "bucket": {
                "name": bucket,
                "ownerIdentity": {"principalId": "oblako"},
                "arn": f"arn:aws:s3:::{bucket}",
            },
            "object": obj,
        },
    }


def _client(service: str):
    """Return a boto3 client for ``service``, on its endpoint override or moto."""
    import boto3

    from oblako import ports

    endpoint = os.environ.get(f"AWS_ENDPOINT_URL_{service.upper()}") or (
        f"http://localhost:{ports.MOTO}"
    )
    return boto3.client(
        service,
        endpoint_url=endpoint,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def deliver(target: Target, payload: dict) -> None:
    """Send one event to a Lambda function, SQS queue or SNS topic."""
    body = json.dumps(payload)
    if target.kind == "lambda":
        name = target.arn.split(":function:", 1)[-1]
        _client("lambda").invoke(FunctionName=name, Payload=body.encode())
    elif target.kind == "sqs":
        sqs = _client("sqs")
        url = sqs.get_queue_url(QueueName=target.arn.rsplit(":", 1)[-1])["QueueUrl"]
        sqs.send_message(QueueUrl=url, MessageBody=body)
    elif target.kind == "sns":
        _client("sns").publish(
            TopicArn=target.arn, Message=body, Subject="Amazon S3 Notification"
        )


def deliver_eventbridge(event: str, bucket: str, key: str, head: dict) -> None:
    """Put the S3 event on the default EventBridge bus, as S3 does."""
    created = event.startswith("s3:ObjectCreated")
    detail = {
        "version": "0",
        "bucket": {"name": bucket},
        "object": {
            "key": key,
            **({"size": int(head.get("content-length", 0))} if head else {}),
        },
        "reason": event.rsplit(":", 1)[-1],
    }
    _client("events").put_events(
        Entries=[
            {
                "Source": "aws.s3",
                "DetailType": "Object Created" if created else "Object Deleted",
                "Resources": [f"arn:aws:s3:::{bucket}"],
                "Detail": json.dumps(detail),
            }
        ]
    )


def _head(backend: str, bucket: str, key: str) -> dict:
    """HEAD the object for its size (best effort: it may be gone already)."""
    url = f"{backend}/{urllib.parse.quote(bucket)}/{urllib.parse.quote(key)}"
    try:
        resp = httpx.head(url, timeout=10)
        return dict(resp.headers) if resp.status_code == 200 else {}
    except httpx.HTTPError:
        return {}


def handle_logged(store, backend: str, entry: dict) -> None:
    """Fire the notifications one completed write matches, if any."""
    if not str(entry.get("status", "")).startswith("2"):
        return  # a failed write: no event, as on S3
    parsed = urllib.parse.urlsplit(entry.get("uri", ""))
    bucket, _, key = urllib.parse.unquote(parsed.path).lstrip("/").partition("/")
    if not bucket or not key:
        return
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    headers = {"x-amz-copy-source": entry["copy"]} if entry.get("copy") else {}
    event = event_name(entry.get("method", ""), query, headers)
    xml = store.notification(bucket) if event else None
    if not xml:
        return
    targets, eventbridge = parse(xml)
    wanted = [t for t in targets if t.matches(event, key)]
    if not wanted and not eventbridge:
        return
    head: dict = {}
    if event == "s3:ObjectCreated:Put" and entry.get("length"):
        head = {"content-length": entry["length"], "etag": entry.get("etag", "")}
    elif event.startswith("s3:ObjectCreated"):
        head = _head(backend, bucket, key) or {"etag": entry.get("etag", "")}
    for target in wanted:
        try:
            deliver(target, {"Records": [record(event, bucket, key, head, target.id)]})
            print(f"s3 event: {event} {bucket}/{key} -> {target.arn}", flush=True)
        except Exception as err:
            print(f"s3 event: {bucket}/{key} -> {target.arn} failed: {err}", flush=True)
    if eventbridge:
        try:
            deliver_eventbridge(event, bucket, key, head)
        except Exception as err:
            print(f"s3 event: {bucket}/{key} -> EventBridge failed: {err}", flush=True)


def log_path() -> Path:
    """Return the write log nginx appends to (see ``oblako.services.s3proxy``)."""
    return Path(
        os.environ.get(
            "OBLAKO_S3_WRITE_LOG",
            str(Path.home() / ".oblako" / "s3-front" / "log" / "writes.log"),
        )
    )


def follow_log(store, backend: str, path: Path | None = None) -> None:
    """Follow nginx's write log forever, handling each new line in order.

    Starts at the current end (writes from before the engine started are not
    replayed) and truncates the file once it's read past 50 MB.
    """
    path = path or log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    offset = path.stat().st_size
    while True:
        size = path.stat().st_size if path.exists() else 0
        if size < offset:  # truncated or replaced
            offset = 0
        if size > offset:
            with open(path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read(size - offset)
            complete, _, _ = chunk.rpartition(b"\n")
            if complete:
                offset += len(complete) + 1
                for line in complete.splitlines():
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    try:
                        handle_logged(store, backend, entry)
                    except Exception as err:
                        print(f"s3 event: {entry.get('uri')}: {err}", flush=True)
            if offset > 50 * 1024 * 1024 and offset == path.stat().st_size:
                with open(path, "wb"):
                    pass
                offset = 0
        time.sleep(0.2)
