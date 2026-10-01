"""S3 Inventory reports for oblako's S3 extensions engine.

A saved ``InventoryConfiguration`` produces what S3 delivers: a data file listing
the source bucket's objects (CSV, gzipped and headerless, or Parquet) and a
``manifest.json`` (+ ``manifest.checksum``) under
``<prefix>/<source-bucket>/<config-id>/`` in the destination bucket. S3 takes up
to 48 hours for the first report; oblako writes it when the configuration is
saved, then every ``OBLAKO_S3_INVENTORY_INTERVAL`` seconds (default: daily).
ORC isn't produced (logged), and Parquet needs pyarrow.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NS = "http://s3.amazonaws.com/doc/2006-03-01/"

# OptionalFields -> (CSV schema name, Parquet column, value from a listed object)
_FIELDS = {
    "Size": ("Size", "size", lambda o: o["Size"]),
    "LastModifiedDate": (
        "LastModifiedDate",
        "last_modified_date",
        lambda o: o["LastModified"],
    ),
    "ETag": ("ETag", "e_tag", lambda o: o["ETag"].strip('"')),
    "StorageClass": ("StorageClass", "storage_class", lambda o: "STANDARD"),
    "IsMultipartUploaded": (
        "IsMultipartUploaded",
        "is_multipart_uploaded",
        lambda o: "-" in o["ETag"],
    ),
    "EncryptionStatus": ("EncryptionStatus", "encryption_status", lambda o: "NOT-SSE"),
}


@dataclass
class Config:
    """The parts of an InventoryConfiguration that shape the report."""

    id: str
    enabled: bool
    destination: str
    format: str
    prefix: str = ""
    filter_prefix: str = ""
    optional_fields: list[str] = field(default_factory=list)
    xml: str = ""


def _strip_ns(elem: ET.Element) -> ET.Element:
    for node in elem.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    return elem


def parse(xml: str | bytes) -> Config:
    """Parse an InventoryConfiguration document."""
    root = _strip_ns(ET.fromstring(xml))
    dest = root.find("Destination/S3BucketDestination")
    if dest is None or not dest.findtext("Bucket") or not dest.findtext("Format"):
        raise ValueError("Destination.S3BucketDestination needs Bucket and Format")
    return Config(
        id=root.findtext("Id") or "",
        enabled=(root.findtext("IsEnabled") or "").lower() == "true",
        destination=dest.findtext("Bucket").split(":::", 1)[-1],
        format=dest.findtext("Format"),
        prefix=(dest.findtext("Prefix") or "").strip("/"),
        filter_prefix=root.findtext("Filter/Prefix") or "",
        optional_fields=[f.text for f in root.iter("Field") if f.text],
    )


def normalize(body: bytes) -> Config:
    """Parse a PUT body and keep it in the namespaced form S3 returns."""
    root = _strip_ns(ET.fromstring(body))
    root.set("xmlns", NS)
    config = parse(ET.tostring(root))
    config.xml = ET.tostring(root, encoding="unicode")
    return config


def _s3():
    import boto3
    from botocore.config import Config as BotoConfig

    from . import backend_url

    return boto3.client(
        "s3",
        endpoint_url=backend_url(),
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=BotoConfig(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def report(source: str, config: Config) -> str:
    """Write one inventory report for ``source``; return the manifest key."""
    if config.format not in ("CSV", "Parquet"):
        raise ValueError(f"inventory format {config.format} isn't produced locally")
    s3 = _s3()
    objects = [
        obj
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=source, Prefix=config.filter_prefix
        )
        for obj in page.get("Contents", [])
    ]
    fields = [f for f in config.optional_fields if f in _FIELDS]
    base = f"{config.prefix}/" if config.prefix else ""
    base += f"{source}/{config.id}"
    if config.format == "CSV":
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
        for obj in objects:
            row = [source, urllib.parse.quote(obj["Key"])]
            for name in fields:
                value = _FIELDS[name][2](obj)
                row.append(value.isoformat() if hasattr(value, "isoformat") else value)
            writer.writerow(row)
        data = gzip.compress(buf.getvalue().encode())
        data_key = f"{base}/data/{uuid.uuid4()}.csv.gz"
        schema = ", ".join(["Bucket", "Key", *[_FIELDS[f][0] for f in fields]])
    else:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as err:
            raise RuntimeError(
                "Parquet inventory reports need pyarrow installed"
            ) from err
        columns = {
            "bucket": [source] * len(objects),
            "key": [o["Key"] for o in objects],
        }
        for name in fields:
            columns[_FIELDS[name][1]] = [_FIELDS[name][2](o) for o in objects]
        sink = io.BytesIO()
        pq.write_table(pa.table(columns), sink)
        data = sink.getvalue()
        data_key = f"{base}/data/{uuid.uuid4()}.parquet"
        schema = "message s3.inventory { " + "; ".join(columns) + " }"
    s3.put_object(Bucket=config.destination, Key=data_key, Body=data)
    stamp = time.strftime("%Y-%m-%dT%H-%MZ", time.gmtime())
    manifest = json.dumps(
        {
            "sourceBucket": source,
            "destinationBucket": f"arn:aws:s3:::{config.destination}",
            "version": "2016-11-30",
            "creationTimestamp": str(int(time.time() * 1000)),
            "fileFormat": config.format,
            "fileSchema": schema,
            "files": [
                {
                    "key": data_key,
                    "size": len(data),
                    "MD5checksum": hashlib.md5(data).hexdigest(),
                }
            ],
        },
        indent=2,
    ).encode()
    manifest_key = f"{base}/{stamp}/manifest.json"
    s3.put_object(Bucket=config.destination, Key=manifest_key, Body=manifest)
    s3.put_object(
        Bucket=config.destination,
        Key=f"{base}/{stamp}/manifest.checksum",
        Body=hashlib.md5(manifest).hexdigest().encode(),
    )
    return manifest_key


def report_safely(source: str, config: Config) -> None:
    """Run :func:`report`, logging instead of raising (it runs in a thread)."""
    try:
        key = report(source, config)
        print(f"s3 inventory: {source}/{config.id} -> {config.destination}/{key}")
    except Exception as err:
        print(f"s3 inventory: {source}/{config.id} failed: {err}", flush=True)


_scheduler_started = False


def start_scheduler(store) -> None:
    """Regenerate every enabled report on a fixed interval (default daily)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    interval = float(os.environ.get("OBLAKO_S3_INVENTORY_INTERVAL", 24 * 3600))

    def loop():
        while True:
            time.sleep(interval)
            for bucket, _, xml in store.all_inventory():
                config = parse(xml)
                if config.enabled:
                    report_safely(bucket, config)

    threading.Thread(target=loop, daemon=True).start()
