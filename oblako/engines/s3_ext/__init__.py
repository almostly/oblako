"""S3 features S3Proxy doesn't implement: tagging, Inventory, policy, notifications.

oblako's S3 is S3Proxy behind a stock nginx on :9000 (see
``oblako.services.s3proxy``). nginx passes every request straight to S3Proxy,
except the ones this engine answers:

- ``?tagging`` on an object or a bucket: Get/Put/DeleteObjectTagging and
  Get/Put/DeleteBucketTagging
- ``?inventory`` on a bucket: Put/Get/List/DeleteBucketInventoryConfiguration
- ``?policy`` / ``?policyStatus`` on a bucket: the bucket policy, stored and
  returned (not enforced; S3Proxy has no IAM)
- ``?notification`` on a bucket: event notifications to Lambda / SQS / SNS /
  EventBridge, fired for the writes nginx logs (see ``notifications``)
- CreateBucket: in us-east-1 S3 answers 200 for a bucket you already own (a
  legacy behavior); S3Proxy answers 409 BucketAlreadyOwnedByYou, passed on
  elsewhere
- requests carrying ``x-amz-tagging`` (PutObject / CreateMultipartUpload with
  ``Tagging=``) or ``x-amz-copy-source`` (CopyObject, which copies or replaces
  tags): forwarded to S3Proxy without the tagging header, then the tags are
  recorded

Tags live in SQLite next to the object's ETag and Last-Modified, the way S3 ties
tags to an object version: overwrite or delete the object and its old tags are
gone. Inventory reports are written to the destination bucket when a
configuration is saved, then daily (see ``inventory``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from oblako import ports
from oblako.engines.identity import claim_port, identify, is_engine

__all__ = ["create_app", "start_in_thread", "is_running", "Store"]

DEFAULT_PORT = ports.S3_EXT
NS = "http://s3.amazonaws.com/doc/2006-03-01/"
CONTROL_NS = "http://awss3control.amazonaws.com/doc/2018-08-20/"
MAX_TAGS = 10


def backend_url() -> str:
    """Return the S3Proxy this engine forwards to and reads objects from."""
    return os.environ.get(
        "OBLAKO_S3_BACKEND", f"http://localhost:{ports.S3_BACKEND}"
    ).rstrip("/")


# -----------------------------------------------------------------------------
# Storage
# -----------------------------------------------------------------------------
class Store:
    """Tags, pending multipart tags and inventory configurations, in SQLite."""

    def __init__(self, path: str | None = None):
        """Open (and create) the database; defaults to ~/.oblako/s3/extensions.db."""
        path = path or os.environ.get(
            "OBLAKO_S3_EXT_DB", str(Path.home() / ".oblako" / "s3" / "extensions.db")
        )
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS object_tags (
                    bucket TEXT, key TEXT, etag TEXT, last_modified TEXT, tags TEXT,
                    PRIMARY KEY (bucket, key));
                CREATE TABLE IF NOT EXISTS pending_tags (
                    upload_id TEXT PRIMARY KEY, bucket TEXT, key TEXT, tags TEXT,
                    created REAL);
                CREATE TABLE IF NOT EXISTS bucket_tags (
                    bucket TEXT PRIMARY KEY, tags TEXT);
                CREATE TABLE IF NOT EXISTS inventory (
                    bucket TEXT, id TEXT, config TEXT, PRIMARY KEY (bucket, id));
                CREATE TABLE IF NOT EXISTS bucket_policy (
                    bucket TEXT PRIMARY KEY, policy TEXT);
                CREATE TABLE IF NOT EXISTS notification (
                    bucket TEXT PRIMARY KEY, config TEXT);
                """
            )

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        """Run one SQL statement, commit, and return its rows."""
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            self._db.commit()
            return rows

    def object_tags(self, bucket: str, key: str) -> tuple[str, str, list] | None:
        """Return (etag, last_modified, tags) recorded for an object, if any."""
        rows = self._q(
            "SELECT etag, last_modified, tags FROM object_tags "
            "WHERE bucket = ? AND key = ?",
            (bucket, key),
        )
        return (rows[0][0], rows[0][1], json.loads(rows[0][2])) if rows else None

    def set_object_tags(self, bucket, key, etag, last_modified, tags) -> None:
        """Record the tags of the object version identified by etag + mtime."""
        self._q(
            "INSERT OR REPLACE INTO object_tags VALUES (?, ?, ?, ?, ?)",
            (bucket, key, etag, last_modified, json.dumps(tags)),
        )

    def delete_object_tags(self, bucket: str, key: str) -> None:
        """Forget an object's tags."""
        self._q("DELETE FROM object_tags WHERE bucket = ? AND key = ?", (bucket, key))

    def add_pending(self, upload_id, bucket, key, tags) -> None:
        """Remember tags given at CreateMultipartUpload until the object exists."""
        self._q(
            "INSERT OR REPLACE INTO pending_tags VALUES (?, ?, ?, ?, ?)",
            (upload_id, bucket, key, json.dumps(tags), time.time()),
        )

    def take_pending(self, bucket: str, key: str) -> list | None:
        """Pop the newest pending multipart tags for an object, if any."""
        rows = self._q(
            "SELECT upload_id, tags FROM pending_tags WHERE bucket = ? AND key = ? "
            "ORDER BY created DESC",
            (bucket, key),
        )
        if not rows:
            return None
        self._q("DELETE FROM pending_tags WHERE bucket = ? AND key = ?", (bucket, key))
        return json.loads(rows[0][1])

    def bucket_tags(self, bucket: str) -> list | None:
        """Return a bucket's tags, or None if it has none."""
        rows = self._q("SELECT tags FROM bucket_tags WHERE bucket = ?", (bucket,))
        return json.loads(rows[0][0]) if rows else None

    def set_bucket_tags(self, bucket: str, tags: list) -> None:
        """Replace a bucket's tags."""
        self._q(
            "INSERT OR REPLACE INTO bucket_tags VALUES (?, ?)",
            (bucket, json.dumps(tags)),
        )

    def delete_bucket_tags(self, bucket: str) -> None:
        """Remove a bucket's tags."""
        self._q("DELETE FROM bucket_tags WHERE bucket = ?", (bucket,))

    def inventory(self, bucket: str, id_: str | None = None) -> list[tuple[str, str]]:
        """Return [(id, config XML)] for a bucket, or one configuration by id."""
        if id_ is None:
            return self._q(
                "SELECT id, config FROM inventory WHERE bucket = ? ORDER BY id",
                (bucket,),
            )
        return self._q(
            "SELECT id, config FROM inventory WHERE bucket = ? AND id = ?",
            (bucket, id_),
        )

    def all_inventory(self) -> list[tuple[str, str, str]]:
        """Return every (bucket, id, config XML)."""
        return self._q("SELECT bucket, id, config FROM inventory")

    def set_inventory(self, bucket: str, id_: str, config: str) -> None:
        """Save an inventory configuration."""
        self._q(
            "INSERT OR REPLACE INTO inventory VALUES (?, ?, ?)", (bucket, id_, config)
        )

    def bucket_policy(self, bucket: str) -> str | None:
        """Return a bucket's policy document, or None."""
        rows = self._q("SELECT policy FROM bucket_policy WHERE bucket = ?", (bucket,))
        return rows[0][0] if rows else None

    def set_bucket_policy(self, bucket: str, policy: str | None) -> None:
        """Replace (or with None, remove) a bucket's policy."""
        if policy is None:
            self._q("DELETE FROM bucket_policy WHERE bucket = ?", (bucket,))
        else:
            self._q(
                "INSERT OR REPLACE INTO bucket_policy VALUES (?, ?)", (bucket, policy)
            )

    def notification(self, bucket: str) -> str | None:
        """Return a bucket's notification configuration XML, or None."""
        rows = self._q("SELECT config FROM notification WHERE bucket = ?", (bucket,))
        return rows[0][0] if rows else None

    def set_notification(self, bucket: str, config: str) -> None:
        """Replace a bucket's notification configuration."""
        self._q("INSERT OR REPLACE INTO notification VALUES (?, ?)", (bucket, config))

    def delete_inventory(self, bucket: str, id_: str) -> int:
        """Delete an inventory configuration; return how many rows went."""
        before = len(self.inventory(bucket, id_))
        self._q("DELETE FROM inventory WHERE bucket = ? AND id = ?", (bucket, id_))
        return before


# -----------------------------------------------------------------------------
# XML and tag helpers
# -----------------------------------------------------------------------------
def _local(tag: str) -> str:
    """Return an XML tag without its ``{namespace}`` prefix."""
    return tag.rsplit("}", 1)[-1]


def _strip_ns(elem: ET.Element) -> ET.Element:
    """Strip XML namespaces from ``elem`` and its descendants."""
    for node in elem.iter():
        node.tag = _local(node.tag)
    return elem


def _error(status: int, code: str, message: str, resource: str = "") -> Response:
    """Return an S3 XML error response."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Error><Code>{code}</Code><Message>{_esc(message)}</Message>"
        f"<Resource>{_esc(resource)}</Resource></Error>"
    )
    return Response(body, status_code=status, media_type="application/xml")


def _esc(text: str) -> str:
    """Escape text for XML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _xml(body: str, status: int = 200) -> Response:
    """Return an XML response with the XML declaration."""
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>' + body,
        status_code=status,
        media_type="application/xml",
    )


def parse_tagset(body: bytes) -> list[dict]:
    """Parse a ``<Tagging><TagSet>`` document into [{"Key", "Value"}]."""
    root = _strip_ns(ET.fromstring(body))
    return [
        {"Key": tag.findtext("Key") or "", "Value": tag.findtext("Value") or ""}
        for tag in root.iter("Tag")
    ]


def parse_tag_header(header: str) -> list[dict]:
    """Parse ``x-amz-tagging`` (URL-encoded ``k=v&k2=v2``)."""
    return [
        {"Key": k, "Value": v}
        for k, v in urllib.parse.parse_qsl(header, keep_blank_values=True)
    ]


def check_tags(tags: list[dict]) -> str | None:
    """Return why S3 would reject these tags, or None if they're valid."""
    if len(tags) > MAX_TAGS:
        return f"Object tags cannot be greater than {MAX_TAGS}"
    keys = [t["Key"] for t in tags]
    if len(set(keys)) != len(keys):
        return "Cannot provide multiple Tags with the same key"
    for tag in tags:
        if not 1 <= len(tag["Key"]) <= 128:
            return "The TagKey you have provided is invalid"
        if len(tag["Value"]) > 256:
            return "The TagValue you have provided is invalid"
    return None


def tagging_xml(tags: list[dict]) -> str:
    """Serialize tags as an S3 ``<Tagging>`` document (no XML declaration)."""
    inner = "".join(
        f"<Tag><Key>{_esc(t['Key'])}</Key><Value>{_esc(t['Value'])}</Value></Tag>"
        for t in tags
    )
    return f'<Tagging xmlns="{NS}"><TagSet>{inner}</TagSet></Tagging>'


def _split(path: str) -> tuple[str, str]:
    """``/bucket/some/key`` -> ("bucket", "some/key")."""
    bucket, _, key = path.lstrip("/").partition("/")
    return bucket, key


def _mtime(header: str | None) -> str:
    """Normalize a Last-Modified header to an ISO timestamp (or "")."""
    if not header:
        return ""
    try:
        return parsedate_to_datetime(header).isoformat()
    except (TypeError, ValueError):
        return header


# -----------------------------------------------------------------------------
# The app
# -----------------------------------------------------------------------------
_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "upgrade",
    "proxy-connection",
    "content-length",
    "host",
}


def is_public(policy: str) -> bool:
    """Return True if a policy allows anyone, unconditionally (as S3 judges it)."""
    statements = json.loads(policy).get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for st in statements:
        principal = st.get("Principal")
        anyone = principal == "*" or (
            isinstance(principal, dict) and "*" in _as_list(principal.get("AWS"))
        )
        if st.get("Effect") == "Allow" and anyone and not st.get("Condition"):
            return True
    return False


def _as_list(value) -> list:
    """Wrap a single value in a list; leave lists as they are."""
    return value if isinstance(value, list) else [value]


def create_app(store: Store | None = None) -> Starlette:
    """Build the ASGI app answering S3 tagging, inventory, policy, notifications."""
    from . import inventory, notifications

    store = store or Store()

    async def head(client: httpx.AsyncClient, path: str) -> httpx.Response:
        """HEAD ``path`` on the backend S3Proxy."""
        return await client.head(backend_url() + urllib.parse.quote(path))

    async def current(client, bucket: str, key: str):
        """Return (etag, last_modified) of the live object, or None if absent."""
        resp = await head(client, f"/{bucket}/{key}")
        if resp.status_code != 200:
            return None
        return resp.headers.get("etag", ""), _mtime(resp.headers.get("last-modified"))

    async def live_tags(client, bucket: str, key: str) -> list | None:
        """Tags of the current object version; None if the object doesn't exist."""
        version = await current(client, bucket, key)
        if version is None:
            return None
        recorded = store.object_tags(bucket, key)
        if recorded and (recorded[0], recorded[1]) == version:
            return recorded[2]
        pending = store.take_pending(bucket, key)  # a completed multipart upload
        if pending is not None:
            etag, modified = version
            store.set_object_tags(bucket, key, etag, modified, pending)
            return pending
        if recorded:  # tags of an overwritten version: gone, as on S3
            store.delete_object_tags(bucket, key)
        return []

    async def forward(client, request: Request, drop: set[str]) -> httpx.Response:
        """Forward the request to the backend, dropping hop-by-hop and ``drop`` headers."""
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _HOP and k.lower() not in drop
        }
        raw = request.scope.get("raw_path", request.url.path.encode()).decode()
        query = request.url.query
        url = backend_url() + raw + (f"?{query}" if query else "")
        body = await request.body()
        return await client.request(request.method, url, headers=headers, content=body)

    def relay(resp: httpx.Response) -> Response:
        """Return the backend's response to the client, minus hop-by-hop headers."""
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP}
        return Response(resp.content, status_code=resp.status_code, headers=headers)

    async def create_bucket(request, client, bucket):
        """CreateBucket, answering for an owned bucket as S3 in us-east-1 does.

        In us-east-1 (no LocationConstraint) S3 answers 200 for a bucket the caller
        already owns, a legacy behavior other Regions don't share; S3Proxy answers
        409 BucketAlreadyOwnedByYou everywhere.
        """
        resp = await forward(client, request, set())
        in_us_east_1 = b"LocationConstraint" not in await request.body()
        if (
            resp.status_code == 409
            and b"BucketAlreadyOwnedByYou" in resp.content
            and in_us_east_1
        ):
            return Response(status_code=200, headers={"Location": f"/{bucket}"})
        return relay(resp)

    async def object_tagging(request, client, bucket, key):
        """Handle Get/Put/DeleteObjectTagging against the live object version."""
        resource = f"/{bucket}/{key}"
        if request.method == "GET":
            tags = await live_tags(client, bucket, key)
            if tags is None:
                return _error(
                    404, "NoSuchKey", "The specified key does not exist.", resource
                )
            return _xml(tagging_xml(tags))
        version = await current(client, bucket, key)
        if version is None:
            return _error(
                404, "NoSuchKey", "The specified key does not exist.", resource
            )
        if request.method == "PUT":
            try:
                tags = parse_tagset(await request.body())
            except ET.ParseError:
                return _error(
                    400, "MalformedXML", "The XML you provided was not well-formed"
                )
            problem = check_tags(tags)
            if problem:
                return _error(400, "InvalidTag", problem, resource)
            etag, modified = version
            store.set_object_tags(bucket, key, etag, modified, tags)
            return Response(status_code=200)
        if request.method == "DELETE":
            store.delete_object_tags(bucket, key)
            return Response(status_code=204)
        return _error(405, "MethodNotAllowed", "The specified method is not allowed.")

    async def bucket_tagging(request, client, bucket):
        """Handle Get/Put/DeleteBucketTagging."""
        if (await head(client, f"/{bucket}")).status_code != 200:
            return _error(
                404, "NoSuchBucket", "The specified bucket does not exist", bucket
            )
        if request.method == "GET":
            tags = store.bucket_tags(bucket)
            if tags is None:
                return _error(404, "NoSuchTagSet", "The TagSet does not exist", bucket)
            return _xml(tagging_xml(tags))
        if request.method == "PUT":
            try:
                tags = parse_tagset(await request.body())
            except ET.ParseError:
                return _error(
                    400, "MalformedXML", "The XML you provided was not well-formed"
                )
            if len(tags) > 50:
                return _error(
                    400, "InvalidTag", "Bucket tags cannot be greater than 50"
                )
            store.set_bucket_tags(bucket, tags)
            return Response(status_code=204)
        if request.method == "DELETE":
            store.delete_bucket_tags(bucket)
            return Response(status_code=204)
        return _error(405, "MethodNotAllowed", "The specified method is not allowed.")

    async def bucket_inventory(request, client, bucket):
        """Handle List/Get/Put/DeleteBucketInventoryConfiguration(s)."""
        if (await head(client, f"/{bucket}")).status_code != 200:
            return _error(
                404, "NoSuchBucket", "The specified bucket does not exist", bucket
            )
        id_ = request.query_params.get("id")
        if request.method == "GET" and not id_:
            configs = "".join(xml for _, xml in store.inventory(bucket))
            return _xml(
                f'<ListInventoryConfigurationsResult xmlns="{NS}">{configs}'
                "<IsTruncated>false</IsTruncated></ListInventoryConfigurationsResult>"
            )
        if not id_:
            return _error(
                400, "InvalidArgument", "Inventory configuration id is required"
            )
        if request.method == "GET":
            rows = store.inventory(bucket, id_)
            if not rows:
                return _error(
                    404,
                    "NoSuchConfiguration",
                    "The specified configuration does not exist.",
                )
            return _xml(rows[0][1])
        if request.method == "PUT":
            try:
                config = inventory.normalize(await request.body())
            except (ET.ParseError, ValueError) as err:
                return _error(400, "MalformedXML", str(err) or "Invalid configuration")
            if config.id != id_:
                return _error(400, "InvalidArgument", "Configuration id doesn't match")
            store.set_inventory(bucket, id_, config.xml)
            if config.enabled:  # the first report, without the 24-48 h wait
                threading.Thread(
                    target=inventory.report_safely, args=(bucket, config), daemon=True
                ).start()
            return Response(status_code=204)
        if request.method == "DELETE":
            if not store.delete_inventory(bucket, id_):
                return _error(
                    404,
                    "NoSuchConfiguration",
                    "The specified configuration does not exist.",
                )
            return Response(status_code=204)
        return _error(405, "MethodNotAllowed", "The specified method is not allowed.")

    async def bucket_policy(request, client, bucket, status_only: bool):
        """Handle Get/Put/DeleteBucketPolicy and GetBucketPolicyStatus."""
        if (await head(client, f"/{bucket}")).status_code != 200:
            return _error(
                404, "NoSuchBucket", "The specified bucket does not exist", bucket
            )
        if request.method == "GET":
            policy = store.bucket_policy(bucket)
            if policy is None:
                return _error(
                    404,
                    "NoSuchBucketPolicy",
                    "The bucket policy does not exist",
                    bucket,
                )
            if status_only:
                public = "true" if is_public(policy) else "false"
                return _xml(
                    f'<PolicyStatus xmlns="{NS}"><IsPublic>{public}</IsPublic>'
                    "</PolicyStatus>"
                )
            return Response(policy, media_type="application/json")
        if request.method == "PUT" and not status_only:
            body = (await request.body()).decode()
            try:
                doc = json.loads(body)
                if not isinstance(doc, dict) or "Statement" not in doc:
                    raise ValueError
            except ValueError:
                return _error(400, "MalformedPolicy", "Policies must be valid JSON")
            store.set_bucket_policy(bucket, body)
            return Response(status_code=204)
        if request.method == "DELETE" and not status_only:
            store.set_bucket_policy(bucket, None)
            return Response(status_code=204)
        return _error(405, "MethodNotAllowed", "The specified method is not allowed.")

    async def bucket_notification(request, client, bucket):
        """Handle Get/PutBucketNotificationConfiguration."""
        if (await head(client, f"/{bucket}")).status_code != 200:
            return _error(
                404, "NoSuchBucket", "The specified bucket does not exist", bucket
            )
        if request.method == "GET":
            return _xml(
                store.notification(bucket)
                or f'<NotificationConfiguration xmlns="{NS}"/>'
            )
        if request.method == "PUT":
            try:
                config = notifications.normalize(await request.body())
            except (ET.ParseError, ValueError) as err:
                return _error(400, "MalformedXML", str(err) or "Invalid configuration")
            store.set_notification(bucket, config)
            return Response(status_code=200)
        return _error(405, "MethodNotAllowed", "The specified method is not allowed.")

    async def write_with_tags(request, client, bucket, key):
        """Forward a tagged PutObject / CreateMultipartUpload / CopyObject, record tags."""
        q = request.query_params
        header = request.headers.get("x-amz-tagging")
        source = request.headers.get("x-amz-copy-source")
        tags: list[dict] = []
        if header is not None:
            tags = parse_tag_header(header)
            problem = check_tags(tags)
            if problem:
                return _error(400, "InvalidTag", problem, f"/{bucket}/{key}")
        if source and "uploadId" not in q:  # CopyObject (not UploadPartCopy)
            directive = request.headers.get("x-amz-tagging-directive", "COPY").upper()
            if directive != "REPLACE":
                src = urllib.parse.unquote(source.split("?", 1)[0]).lstrip("/")
                tags = await live_tags(client, *_split("/" + src)) or []
        resp = await forward(
            client, request, {"x-amz-tagging", "x-amz-tagging-directive"}
        )
        if (
            resp.status_code >= 300
            or (header is None and not source)
            or "uploadId" in q
        ):
            return relay(resp)
        if request.method == "POST" and "uploads" in q:  # CreateMultipartUpload
            upload_id = _strip_ns(ET.fromstring(resp.content)).findtext("UploadId")
            if upload_id:
                store.add_pending(upload_id, bucket, key, tags)
            return relay(resp)
        version = await current(client, bucket, key)
        if version is not None:
            etag, modified = version
            store.set_object_tags(bucket, key, etag, modified, tags)
        return relay(resp)

    async def control_tags(request: Request) -> Response:
        """S3 Control's TagResource / UntagResource / ListTagsForResource on a bucket.

        The same tags as Get/PutBucketTagging: the AWS SDKs for Go (Terraform,
        Pulumi) read and write a bucket's tags through S3 Control.
        """
        arn = urllib.parse.unquote(request.path_params["arn"])
        if ":::" in arn:
            bucket = arn.split(":::", 1)[1]
        else:
            bucket = arn.rsplit("/", 1)[-1]
        tags = store.bucket_tags(bucket) or []
        if request.method == "GET":
            inner = "".join(
                f"<Tag><Key>{_esc(t['Key'])}</Key><Value>{_esc(t['Value'])}</Value></Tag>"
                for t in tags
            )
            return _xml(
                f'<ListTagsForResourceResult xmlns="{CONTROL_NS}">'
                f"<Tags>{inner}</Tags></ListTagsForResourceResult>"
            )
        if request.method == "POST":
            new = parse_tagset(await request.body())
            merged = {t["Key"]: t["Value"] for t in tags}
            merged.update({t["Key"]: t["Value"] for t in new})
            tags = [{"Key": k, "Value": v} for k, v in merged.items()]
        else:  # DELETE
            gone = set(request.query_params.getlist("tagKeys"))
            tags = [t for t in tags if t["Key"] not in gone]
        problem = check_tags(tags)
        if problem:
            return _error(400, "InvalidTag", problem)
        if tags:
            store.set_bucket_tags(bucket, tags)
        else:
            store.delete_bucket_tags(bucket)
        return Response(status_code=204)

    async def handle(request: Request) -> Response:
        """Route an S3 request to its extension handler, or relay it to the backend."""
        bucket, key = _split(request.url.path)
        q = request.query_params
        async with httpx.AsyncClient(timeout=None) as client:
            if not bucket:
                return relay(await forward(client, request, set()))
            if request.method == "PUT" and not key and not q:
                return await create_bucket(request, client, bucket)
            if "tagging" in q:
                if key:
                    return await object_tagging(request, client, bucket, key)
                return await bucket_tagging(request, client, bucket)
            if "inventory" in q and not key:
                return await bucket_inventory(request, client, bucket)
            if ("policy" in q or "policyStatus" in q) and not key:
                return await bucket_policy(request, client, bucket, "policyStatus" in q)
            if "notification" in q and not key:
                return await bucket_notification(request, client, bucket)
            if key and (
                "x-amz-tagging" in request.headers
                or "x-amz-copy-source" in request.headers
            ):
                return await write_with_tags(request, client, bucket, key)
            return relay(await forward(client, request, set()))

    methods = ["GET", "PUT", "POST", "DELETE", "HEAD"]
    app = Starlette(
        routes=[
            Route(
                "/v20180820/tags/{arn:path}",
                control_tags,
                methods=["GET", "POST", "DELETE"],
            ),
            Route("/", handle, methods=methods),
            Route("/{path:path}", handle, methods=methods),
        ]
    )
    app.state.store = store
    inventory.start_scheduler(store)
    _start_event_follower(store)
    return app


_follower_started = False


def _start_event_follower(store: Store) -> None:
    """Follow nginx's write log once per process (see ``notifications``)."""
    global _follower_started
    if _follower_started:
        return
    _follower_started = True
    from . import notifications

    threading.Thread(
        target=notifications.follow_log, args=(store, backend_url()), daemon=True
    ).start()


_servers: dict[int, object] = {}
_lock = threading.Lock()


def is_running(port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True if the S3 extensions engine is reachable on the port."""
    return is_engine(port, "s3_ext", timeout)


def start_in_thread(port: int = DEFAULT_PORT, store: Store | None = None) -> str:
    """Start the S3 extensions engine in a daemon thread (idempotent)."""
    import uvicorn

    url = f"http://localhost:{port}"
    if is_running(port):
        return url
    with _lock:
        if port in _servers:
            return url
        claim_port(port, "s3_ext")
        config = uvicorn.Config(
            identify(create_app(store), "s3_ext"),
            host="0.0.0.0",  # nginx reaches it from its container
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        _servers[port] = server
    deadline = time.time() + 10
    while time.time() < deadline:
        if is_running(port):
            return url
        time.sleep(0.1)
    raise RuntimeError(f"s3 extensions engine did not start on port {port}")
