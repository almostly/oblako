"""Glue crawlers and classifiers on the Glue engine.

A crawler reads its S3 targets on oblako's S3 and writes what it finds into the
Data Catalog, as Glue does:

* one table per data folder, named after it (with ``TablePrefix``); folders whose
  files share a format and columns are one table partitioned by the folder
  level, ``key=value`` folders give the partition key's name and others are
  ``partition_0``, ``partition_1`` ...;
* the schema from the files: a Parquet footer, a CSV header (sniffed, or from a
  custom CSV classifier) with column types inferred from the values, or the keys
  of JSON lines; the table's SerDe, input/output formats and ``classification``
  match what Glue writes;
* every partition registered with its location;
* ``SchemaChangePolicy``: ``UPDATE_IN_DATABASE`` (default) updates an existing
  table's columns, ``LOG`` leaves them; a table the crawler made whose folder is
  gone is deprecated (``DEPRECATE_IN_DATABASE``) or deleted
  (``DELETE_FROM_DATABASE``);
* ``Schedule`` (``cron(...)``) starts the crawler on its own.

A crawl runs in a background thread: ``StartCrawler`` returns at once, the
crawler is ``RUNNING`` then ``STOPPING`` then ``READY``, and ``LastCrawl`` and
``GetCrawlerMetrics`` report the result. Classifiers (CSV, JSON, Grok, XML) are
stored as Glue stores them; crawls apply the CSV ones (delimiter, quote, header)
and the JSON ones' ``JsonPath`` of ``$[*]`` (a top-level array of records). Grok and
XML classifiers are kept but not applied. Parquet needs pyarrow.

Definitions and crawl results are kept in ``~/.oblako/glue/crawlers.json``.
"""

from __future__ import annotations

import csv
import fnmatch
import io
import json
import re
import threading
import time
from pathlib import Path

from oblako import ports
from oblako.engines.glue_catalog import _ACTIONS, GlueError, _action, _not_found

STATE = Path.home() / ".oblako" / "glue" / "crawlers.json"
_lock = threading.RLock()
_CLASSIFIER_KINDS = (
    "CsvClassifier",
    "JsonClassifier",
    "GrokClassifier",
    "XMLClassifier",
)


# ---------------------------------------------------------------------------
# State: {"crawlers": {name: crawler}, "classifiers": {name: classifier},
#         "metrics": {name: {...}}}
# ---------------------------------------------------------------------------
def _load() -> dict:
    """Return the crawler state from disk, with every section present."""
    if STATE.exists():
        state = json.loads(STATE.read_text())
    else:
        state = {}
    for key in ("crawlers", "classifiers", "metrics"):
        state.setdefault(key, {})
    return state


def _save(state: dict) -> None:
    """Write the crawler state to disk atomically."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str))
    tmp.replace(STATE)


def _require(state: dict, kind: str, name: str) -> dict:
    """Return a crawler or classifier, raising EntityNotFoundException if there is none."""
    item = state[kind].get(name)
    if item is None:
        label = "Crawler" if kind == "crawlers" else "Classifier"
        raise _not_found(f"{label} with name {name} not found")
    return item


def _update_crawler(name: str, **fields) -> None:
    """Set fields on a stored crawler, if it still exists."""
    with _lock:
        state = _load()
        if name in state["crawlers"]:
            state["crawlers"][name].update(fields)
            _save(state)


# ---------------------------------------------------------------------------
# Classifiers
# ---------------------------------------------------------------------------
def _classifier_body(body: dict) -> tuple[str, dict]:
    """Return the request's one classifier kind and its spec."""
    kinds = [k for k in _CLASSIFIER_KINDS if body.get(k)]
    if len(kinds) != 1:
        raise GlueError(
            "InvalidInputException", "Exactly one classifier type is required"
        )
    return kinds[0], dict(body[kinds[0]])


@_action("AWSGlue.CreateClassifier")
def _create_classifier(body):
    """CreateClassifier: store a new classifier."""
    kind, spec = _classifier_body(body)
    with _lock:
        state = _load()
        if spec["Name"] in state["classifiers"]:
            raise GlueError(
                "AlreadyExistsException", f"Classifier {spec['Name']} already exists"
            )
        now = time.time()
        state["classifiers"][spec["Name"]] = {
            kind: {**spec, "CreationTime": now, "LastUpdated": now, "Version": 1}
        }
        _save(state)
    return {}


@_action("AWSGlue.UpdateClassifier")
def _update_classifier(body):
    """UpdateClassifier: merge a classifier's new spec and bump its version."""
    kind, spec = _classifier_body(body)
    with _lock:
        state = _load()
        current = _require(state, "classifiers", spec["Name"])
        if kind not in current:
            raise GlueError("InvalidInputException", f"{spec['Name']} is not a {kind}")
        old = current[kind]
        current[kind] = {
            **old,
            **spec,
            "LastUpdated": time.time(),
            "Version": old.get("Version", 1) + 1,
        }
        _save(state)
    return {}


@_action("AWSGlue.GetClassifier")
def _get_classifier(body):
    """GetClassifier: return one classifier by name."""
    return {"Classifier": _require(_load(), "classifiers", body["Name"])}


@_action("AWSGlue.GetClassifiers")
def _get_classifiers(_body):
    """GetClassifiers: return every classifier."""
    return {"Classifiers": list(_load()["classifiers"].values())}


@_action("AWSGlue.DeleteClassifier")
def _delete_classifier(body):
    """DeleteClassifier: remove one classifier."""
    with _lock:
        state = _load()
        _require(state, "classifiers", body["Name"])
        del state["classifiers"][body["Name"]]
        _save(state)
    return {}


# ---------------------------------------------------------------------------
# Crawlers: definitions
# ---------------------------------------------------------------------------
_DEFINITION = (
    "Role",
    "DatabaseName",
    "Description",
    "Targets",
    "Classifiers",
    "TablePrefix",
    "SchemaChangePolicy",
    "RecrawlPolicy",
    "LineageConfiguration",
    "LakeFormationConfiguration",
    "Configuration",
    "CrawlerSecurityConfiguration",
)


def _schedule(expression: str | None) -> dict | None:
    """Return a crawler's Schedule for a cron expression, or None."""
    if not expression:
        return None
    return {"ScheduleExpression": expression, "State": "SCHEDULED"}


@_action("AWSGlue.CreateCrawler")
def _create_crawler(body):
    """CreateCrawler: store a new crawler in the READY state."""
    name = body["Name"]
    with _lock:
        state = _load()
        if name in state["crawlers"]:
            raise GlueError("AlreadyExistsException", f"Crawler {name} already exists")
        now = time.time()
        crawler = {k: body[k] for k in _DEFINITION if k in body}
        crawler.update(
            Name=name,
            State="READY",
            CreationTime=now,
            LastUpdated=now,
            Version=1,
            CrawlElapsedTime=0,
        )
        crawler.setdefault(
            "SchemaChangePolicy",
            {
                "UpdateBehavior": "UPDATE_IN_DATABASE",
                "DeleteBehavior": "DEPRECATE_IN_DATABASE",
            },
        )
        crawler.setdefault("RecrawlPolicy", {"RecrawlBehavior": "CRAWL_EVERYTHING"})
        if schedule := _schedule(body.get("Schedule")):
            crawler["Schedule"] = schedule
        state["crawlers"][name] = crawler
        _save(state)
    return {}


@_action("AWSGlue.UpdateCrawler")
def _update_crawler_action(body):
    """UpdateCrawler: change a READY crawler's definition and bump its version."""
    name = body["Name"]
    with _lock:
        state = _load()
        crawler = _require(state, "crawlers", name)
        if crawler["State"] != "READY":
            raise GlueError(
                "CrawlerRunningException", f"Crawler with name {name} is running"
            )
        crawler.update({k: body[k] for k in _DEFINITION if k in body})
        if "Schedule" in body:
            crawler["Schedule"] = _schedule(body["Schedule"])
        crawler["LastUpdated"] = time.time()
        crawler["Version"] = crawler.get("Version", 1) + 1
        _save(state)
    return {}


@_action("AWSGlue.UpdateCrawlerSchedule")
def _update_crawler_schedule(body):
    """UpdateCrawlerSchedule: set or clear a crawler's schedule."""
    with _lock:
        state = _load()
        crawler = _require(state, "crawlers", body["CrawlerName"])
        crawler["Schedule"] = _schedule(body.get("Schedule"))
        _save(state)
    return {}


@_action("AWSGlue.StartCrawlerSchedule")
def _start_crawler_schedule(body):
    """StartCrawlerSchedule: set a crawler's schedule to SCHEDULED."""
    return _set_schedule_state(body["CrawlerName"], "SCHEDULED")


@_action("AWSGlue.StopCrawlerSchedule")
def _stop_crawler_schedule(body):
    """StopCrawlerSchedule: set a crawler's schedule to NOT_SCHEDULED."""
    return _set_schedule_state(body["CrawlerName"], "NOT_SCHEDULED")


def _set_schedule_state(name: str, value: str) -> dict:
    """Set the state of a crawler's schedule, raising NoScheduleException if it has none."""
    with _lock:
        state = _load()
        crawler = _require(state, "crawlers", name)
        if not crawler.get("Schedule"):
            raise GlueError("NoScheduleException", f"Crawler {name} has no schedule")
        crawler["Schedule"]["State"] = value
        _save(state)
    return {}


@_action("AWSGlue.GetCrawler")
def _get_crawler(body):
    """GetCrawler: return one crawler by name."""
    return {"Crawler": _require(_load(), "crawlers", body["Name"])}


@_action("AWSGlue.GetCrawlers")
def _get_crawlers(_body):
    """GetCrawlers: return every crawler."""
    return {"Crawlers": list(_load()["crawlers"].values())}


@_action("AWSGlue.ListCrawlers")
def _list_crawlers(_body):
    """ListCrawlers: return the crawler names."""
    return {"CrawlerNames": sorted(_load()["crawlers"])}


@_action("AWSGlue.BatchGetCrawlers")
def _batch_get_crawlers(body):
    """BatchGetCrawlers: return the named crawlers and the ones not found."""
    crawlers = _load()["crawlers"]
    names = body.get("CrawlerNames") or []
    return {
        "Crawlers": [crawlers[n] for n in names if n in crawlers],
        "CrawlersNotFound": [n for n in names if n not in crawlers],
    }


@_action("AWSGlue.DeleteCrawler")
def _delete_crawler(body):
    """DeleteCrawler: remove a READY crawler and its metrics."""
    name = body["Name"]
    with _lock:
        state = _load()
        crawler = _require(state, "crawlers", name)
        if crawler["State"] != "READY":
            raise GlueError(
                "CrawlerRunningException", f"Crawler with name {name} is running"
            )
        del state["crawlers"][name]
        state["metrics"].pop(name, None)
        _save(state)
    return {}


@_action("AWSGlue.GetCrawlerMetrics")
def _get_crawler_metrics(body):
    """GetCrawlerMetrics: return the last crawl's runtimes and table counts."""
    state = _load()
    names = body.get("CrawlerNameList") or sorted(state["crawlers"])
    out = []
    for name in names:
        if name not in state["crawlers"]:
            continue
        m = state["metrics"].get(name, {})
        out.append(
            {
                "CrawlerName": name,
                "TimeLeftSeconds": 0.0,
                "StillEstimating": False,
                "LastRuntimeSeconds": m.get("LastRuntimeSeconds", 0.0),
                "MedianRuntimeSeconds": m.get("MedianRuntimeSeconds", 0.0),
                "TablesCreated": m.get("TablesCreated", 0),
                "TablesUpdated": m.get("TablesUpdated", 0),
                "TablesDeleted": m.get("TablesDeleted", 0),
            }
        )
    return {"CrawlerMetricsList": out}


# ---------------------------------------------------------------------------
# Crawlers: running
# ---------------------------------------------------------------------------
_STOP: set[str] = set()


@_action("AWSGlue.StartCrawler")
def _start_crawler(body):
    """StartCrawler: start a crawl in the background."""
    start(body["Name"])
    return {}


def start(name: str) -> None:
    """Start a crawl in the background (StartCrawler, a schedule, a workflow)."""
    with _lock:
        state = _load()
        crawler = _require(state, "crawlers", name)
        if crawler["State"] != "READY":
            raise GlueError(
                "CrawlerRunningException",
                f"Crawler with name {name} has already started",
            )
        crawler["State"] = "RUNNING"
        crawler["LastCrawlStart"] = time.time()
        _save(state)
    _STOP.discard(name)
    threading.Thread(target=_run, args=(name,), daemon=True).start()


@_action("AWSGlue.StopCrawler")
def _stop_crawler(body):
    """StopCrawler: ask a running crawl to stop."""
    name = body["Name"]
    with _lock:
        state = _load()
        crawler = _require(state, "crawlers", name)
        if crawler["State"] != "RUNNING":
            raise GlueError(
                "CrawlerNotRunningException", f"Crawler with name {name} is not running"
            )
        crawler["State"] = "STOPPING"
        _save(state)
    _STOP.add(name)
    return {}


class _Stopped(Exception):
    """The crawl was stopped (StopCrawler)."""


def _run(name: str) -> None:
    """Crawl in the background, then record LastCrawl and the metrics."""
    started = time.time()
    crawler = _load()["crawlers"][name]
    status, error, counts = (
        "SUCCEEDED",
        None,
        {"created": 0, "updated": 0, "deleted": 0},
    )
    try:
        counts = crawl(crawler)
    except _Stopped:
        status = "CANCELLED"
    except Exception as e:  # the crawl's failure, reported as Glue does
        status, error = "FAILED", str(e)[:2000]
    elapsed = time.time() - started
    last = {
        "Status": status,
        "LogGroup": "/aws-glue/crawlers",
        "LogStream": name,
        "MessagePrefix": f"{name}-{int(started)}",
        "StartTime": started,
    }
    if error:
        last["ErrorMessage"] = error
    _update_crawler(name, State="STOPPING")
    with _lock:
        state = _load()
        if name in state["crawlers"]:
            state["crawlers"][name].update(
                State="READY", LastCrawl=last, CrawlElapsedTime=int(elapsed * 1000)
            )
            runs = state["metrics"].get(name, {}).get("runs", []) + [elapsed]
            state["metrics"][name] = {
                "runs": runs[-50:],
                "LastRuntimeSeconds": elapsed,
                "MedianRuntimeSeconds": sorted(runs)[len(runs) // 2],
                "TablesCreated": counts["created"],
                "TablesUpdated": counts["updated"],
                "TablesDeleted": counts["deleted"],
            }
            _save(state)


def _check_stop(name: str) -> None:
    """Raise _Stopped if StopCrawler was called for the crawler."""
    if name in _STOP:
        raise _Stopped()


# ---------------------------------------------------------------------------
# Reading S3 and inferring tables
# ---------------------------------------------------------------------------
_HIVE = re.compile(r"^([^=/]+)=(.*)$")
_PARQUET = {
    "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
    "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
    "SerdeInfo": {
        "SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
        "Parameters": {"serialization.format": "1"},
    },
}
_TEXT = {
    "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
    "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
}


def _s3():
    """Return a boto3 S3 client for oblako's S3."""
    from oblako.services.boto import client

    return client("s3", f"http://localhost:{ports.S3}")


def _list(s3, path: str, exclusions: list[str]) -> list[dict]:
    """Return the data objects under an s3:// path, without Glue's skipped files."""
    bucket, _, prefix = path.removeprefix("s3://").partition("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    objects = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix) :]
            name = rel.rsplit("/", 1)[-1]
            if not name or name.startswith(("_", ".")) or name.endswith("_$folder$"):
                continue  # directory markers, _SUCCESS, hidden files
            if any(fnmatch.fnmatch(rel, pattern) for pattern in exclusions):
                continue
            objects.append(
                {"Bucket": bucket, "Key": obj["Key"], "Rel": rel, "Size": obj["Size"]}
            )
    return objects


def _format_of(key: str, head: bytes) -> str:
    """Return a file's format (parquet, json or csv) from its key and first bytes."""
    if head.startswith(b"PAR1") or key.endswith(".parquet"):
        return "parquet"
    stripped = head.lstrip()
    if stripped.startswith((b"{", b"[")):
        return "json"
    return "csv"


def _glue_type_of_arrow(t) -> str:
    """Return the Glue/Hive type for an Arrow type."""
    import pyarrow as pa

    if pa.types.is_boolean(t):
        return "boolean"
    if pa.types.is_int8(t) or pa.types.is_uint8(t):
        return "tinyint"
    if pa.types.is_int16(t) or pa.types.is_uint16(t):
        return "smallint"
    if pa.types.is_int32(t) or pa.types.is_uint32(t):
        return "int"
    if pa.types.is_integer(t):
        return "bigint"
    if pa.types.is_float16(t) or pa.types.is_float32(t):
        return "float"
    if pa.types.is_floating(t):
        return "double"
    if pa.types.is_decimal(t):
        return f"decimal({t.precision},{t.scale})"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_timestamp(t):
        return "timestamp"
    if pa.types.is_binary(t) or pa.types.is_large_binary(t):
        return "binary"
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        return f"array<{_glue_type_of_arrow(t.value_type)}>"
    if pa.types.is_map(t):
        return (
            f"map<{_glue_type_of_arrow(t.key_type)},{_glue_type_of_arrow(t.item_type)}>"
        )
    if pa.types.is_struct(t):
        inner = ",".join(f"{f.name}:{_glue_type_of_arrow(f.type)}" for f in t)
        return f"struct<{inner}>"
    return "string"


def _value_type(values: list[str]) -> str:
    """Glue's type for a CSV column from its values: bigint, double, boolean, string."""
    present = [v for v in values if v != ""]
    if not present:
        return "string"
    if all(re.fullmatch(r"[-+]?\d+", v) for v in present):
        return "bigint"
    if all(re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", v) for v in present):
        return "double"
    if all(v.lower() in ("true", "false") for v in present):
        return "boolean"
    return "string"


def _json_type(value) -> str:
    """Return the Glue/Hive type for a JSON value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "bigint"
    if isinstance(value, float):
        return "double"
    if isinstance(value, list):
        inner = _json_type(value[0]) if value else "string"
        return f"array<{inner}>"
    if isinstance(value, dict):
        return (
            "struct<" + ",".join(f"{k}:{_json_type(v)}" for k, v in value.items()) + ">"
        )
    return "string"


def _csv_settings(classifiers: list[dict]) -> dict:
    """Return the first CSV classifier's settings, or {}."""
    for c in classifiers:
        if "CsvClassifier" in c:
            return c["CsvClassifier"]
    return {}


def _json_path(classifiers: list[dict]) -> str | None:
    """Return the first JSON classifier's JsonPath, or None."""
    for c in classifiers:
        if "JsonClassifier" in c:
            return c["JsonClassifier"].get("JsonPath")
    return None


def _schema_of(s3, obj: dict, classifiers: list[dict]) -> dict:
    """Infer one file's format and columns: {"format", "columns", "params", "rows"}."""
    body = s3.get_object(Bucket=obj["Bucket"], Key=obj["Key"])["Body"].read()
    fmt = _format_of(obj["Key"], body[:64])
    if fmt == "parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError as e:
            raise RuntimeError(
                "crawling Parquet needs pyarrow: pip install pyarrow"
            ) from e
        meta = pq.ParquetFile(io.BytesIO(body))
        schema = meta.schema_arrow
        columns = [
            {"Name": f.name.lower(), "Type": _glue_type_of_arrow(f.type)}
            for f in schema
        ]
        return {
            "format": "parquet",
            "columns": columns,
            "params": {},
            "rows": meta.metadata.num_rows,
        }
    text = body.decode("utf-8", errors="replace")
    if fmt == "json":
        records: list = []
        if _json_path(classifiers) == "$[*]" or text.lstrip().startswith("["):
            parsed = json.loads(text)
            records = parsed if isinstance(parsed, list) else [parsed]
        else:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
        keys: dict[str, str] = {}
        for rec in records[:1000]:
            for k, v in rec.items():
                if v is not None:
                    keys.setdefault(k.lower(), _json_type(v))
        columns = [{"Name": k, "Type": t} for k, t in keys.items()]
        return {
            "format": "json",
            "columns": columns,
            "params": {},
            "rows": len(records),
        }
    settings = _csv_settings(classifiers)
    sample = text[:65536]
    delimiter = settings.get("Delimiter")
    if not delimiter:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",|\t;").delimiter
        except csv.Error:
            delimiter = ","
    quote = settings.get("QuoteSymbol") or '"'
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter, quotechar=quote))
    rows = [r for r in rows if r]
    contains = settings.get("ContainsHeader", "UNKNOWN")
    if contains == "PRESENT":
        header = True
    elif contains == "ABSENT":
        header = False
    else:
        try:
            header = csv.Sniffer().has_header(sample)
        except csv.Error:
            header = False
    if settings.get("Header"):
        names, data = (
            [h.lower() for h in settings["Header"]],
            rows[1:] if header else rows,
        )
    elif header and rows:
        names, data = [h.strip().lower() for h in rows[0]], rows[1:]
    else:
        width = max((len(r) for r in rows), default=0)
        names, data = [f"col{i}" for i in range(width)], rows
    columns = [
        {
            "Name": n,
            "Type": _value_type([r[i] if i < len(r) else "" for r in data[:1000]]),
        }
        for i, n in enumerate(names)
    ]
    params = {
        "delimiter": delimiter,
        "areColumnsQuoted": "false",
        "columnsOrdered": "true",
    }
    if header:
        params["skip.header.line.count"] = "1"
    return {"format": "csv", "columns": columns, "params": params, "rows": len(data)}


def _merge_columns(schemas: list[dict]) -> list[dict]:
    """Return the columns of all schemas, the first type of each name kept."""
    seen: dict[str, str] = {}
    for s in schemas:
        for c in s["columns"]:
            seen.setdefault(c["Name"], c["Type"])
    return [{"Name": n, "Type": t} for n, t in seen.items()]


def _compatible(a: dict, b: dict) -> bool:
    """Return True if two schemas have the same format and column names."""
    return a["format"] == b["format"] and [c["Name"] for c in a["columns"]] == [
        c["Name"] for c in b["columns"]
    ]


def _tables(s3, root: str, objects: list[dict], classifiers: list[dict]) -> list[dict]:
    """Group a target's files into tables: [{"root", "files", "schema", "partitions"}].

    Files whose folders below the root are all key=value folders, or whose
    first-level folders hold the same format and columns, are one table; folders
    that differ are tables of their own, found the same way below them. A lone
    dataset folder under the root is a table named after it.
    """
    if not objects:
        return []
    first = {o["Rel"].split("/", 1)[0] for o in objects if "/" in o["Rel"]}
    direct = any("/" not in o["Rel"] for o in objects)
    if not direct and len(first) == 1 and not _HIVE.match(next(iter(first))):
        # a lone dataset folder is a table of its own, named after it
        (folder,) = first
        sub = [{**o, "Rel": o["Rel"][len(folder) + 1 :]} for o in objects]
        return _tables(s3, root.rstrip("/") + "/" + folder + "/", sub, classifiers)
    schemas = {}
    for o in objects:
        folder = o["Rel"].split("/", 1)[0] if "/" in o["Rel"] else ""
        if folder not in schemas:
            schemas[folder] = _schema_of(s3, o, classifiers)
    one = list(schemas.values())
    single = all(_HIVE.match(f) for f in first) or all(
        _compatible(one[0], s) for s in one[1:]
    )
    if single:
        return [{"root": root, "files": objects, "schemas": one}]
    tables = []
    for folder in sorted(first):
        sub = [
            {**o, "Rel": o["Rel"][len(folder) + 1 :]}
            for o in objects
            if o["Rel"].startswith(folder + "/")
        ]
        tables += _tables(s3, root.rstrip("/") + "/" + folder + "/", sub, classifiers)
    return tables


def _table_name(prefix: str, root: str) -> str:
    """Return the table name for a data folder, with the crawler's prefix."""
    base = (
        root.rstrip("/").rsplit("/", 1)[-1] or root.removeprefix("s3://").split("/")[0]
    )
    return re.sub(r"[^a-z0-9_]", "_", (prefix + base).lower())


def _partition_layout(files: list[dict]) -> tuple[list[str], dict[tuple, str]]:
    """Return the partition keys and {values: folder} for a table's files."""
    depth = max((len(f["Rel"].split("/")) - 1 for f in files), default=0)
    keys = [f"partition_{i}" for i in range(depth)]
    found: dict[tuple, str] = {}
    for f in files:
        folders = f["Rel"].split("/")[:-1]
        if len(folders) != depth:
            continue
        values = []
        for i, folder in enumerate(folders):
            m = _HIVE.match(folder)
            if m:
                keys[i] = m.group(1).lower()
                values.append(m.group(2))
            else:
                values.append(folder)
        found[tuple(values)] = "/".join(folders) + "/"
    return keys, found


def crawl(crawler: dict) -> dict[str, int]:
    """Crawl every S3 target and write the tables; return created/updated/deleted."""
    name, database = crawler["Name"], crawler["DatabaseName"]
    state = _load()
    classifiers = [
        state["classifiers"][c]
        for c in crawler.get("Classifiers") or []
        if c in state["classifiers"]
    ]
    try:
        _ACTIONS["AWSGlue.GetDatabase"]({"Name": database})
    except GlueError:
        raise RuntimeError(f"Database {database} not found") from None
    policy = crawler.get("SchemaChangePolicy") or {}
    update = policy.get("UpdateBehavior", "UPDATE_IN_DATABASE")
    delete = policy.get("DeleteBehavior", "DEPRECATE_IN_DATABASE")
    s3 = _s3()
    counts = {"created": 0, "updated": 0, "deleted": 0}
    seen: set[str] = set()
    for target in (crawler.get("Targets") or {}).get("S3Targets") or []:
        _check_stop(name)
        path = target["Path"].rstrip("/") + "/"
        objects = _list(s3, path, target.get("Exclusions") or [])
        for table in _tables(s3, path, objects, classifiers):
            _check_stop(name)
            table_name = _table_name(crawler.get("TablePrefix") or "", table["root"])
            seen.add(table_name)
            schema = table["schemas"][0]
            keys, partitions = _partition_layout(table["files"])
            columns = [
                c for c in _merge_columns(table["schemas"]) if c["Name"] not in keys
            ]
            params = {
                "classification": schema["format"],
                "compressionType": "none",
                "typeOfData": "file",
                "objectCount": str(len(table["files"])),
                "recordCount": str(schema["rows"]),
                "sizeKey": str(sum(f["Size"] for f in table["files"])),
                "CrawlerSchemaDeserializerVersion": "1.0",
                "CrawlerSchemaSerializerVersion": "1.0",
                "UPDATED_BY_CRAWLER": name,
                **schema["params"],
            }
            if schema["format"] == "parquet":
                formats = _PARQUET
            elif schema["format"] == "json":
                formats = {
                    **_TEXT,
                    "SerdeInfo": {
                        "SerializationLibrary": "org.openx.data.jsonserde.JsonSerDe",
                        "Parameters": {"paths": ",".join(c["Name"] for c in columns)},
                    },
                }
            else:
                formats = {
                    **_TEXT,
                    "SerdeInfo": {
                        "SerializationLibrary": "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe",
                        "Parameters": {"field.delim": schema["params"]["delimiter"]},
                    },
                }
            storage = {
                "Columns": columns,
                "Location": table["root"],
                **formats,
                "Compressed": False,
                "NumberOfBuckets": -1,
                "StoredAsSubDirectories": False,
                "Parameters": params,
            }
            table_input = {
                "Name": table_name,
                "StorageDescriptor": storage,
                "PartitionKeys": [{"Name": k, "Type": "string"} for k in keys],
                "TableType": "EXTERNAL_TABLE",
                "Parameters": params,
            }
            try:
                _ACTIONS["AWSGlue.GetTable"](
                    {"DatabaseName": database, "Name": table_name}
                )
                exists = True
            except GlueError:
                exists = False
            if not exists:
                _ACTIONS["AWSGlue.CreateTable"](
                    {"DatabaseName": database, "TableInput": table_input}
                )
                counts["created"] += 1
            elif update == "UPDATE_IN_DATABASE":
                _ACTIONS["AWSGlue.UpdateTable"](
                    {"DatabaseName": database, "TableInput": table_input}
                )
                counts["updated"] += 1
            _add_partitions(database, table_name, storage, partitions)
    counts["deleted"] = _retire(database, name, seen, delete)
    return counts


def _add_partitions(database: str, table: str, storage: dict, partitions: dict) -> None:
    """Register the partitions the table doesn't have yet, 100 at a time."""
    existing = {
        tuple(p["Values"])
        for p in _ACTIONS["AWSGlue.GetPartitions"](
            {"DatabaseName": database, "TableName": table}
        )["Partitions"]
    }
    new = [
        {
            "Values": list(values),
            "StorageDescriptor": {**storage, "Location": storage["Location"] + folder},
        }
        for values, folder in sorted(partitions.items())
        if values and values not in existing
    ]
    for start in range(0, len(new), 100):
        _ACTIONS["AWSGlue.BatchCreatePartition"](
            {
                "DatabaseName": database,
                "TableName": table,
                "PartitionInputList": new[start : start + 100],
            }
        )


def _retire(database: str, crawler: str, seen: set[str], behavior: str) -> int:
    """Deprecate or delete tables this crawler made whose data is gone."""
    if behavior == "LOG":
        return 0
    gone = 0
    for table in _ACTIONS["AWSGlue.GetTables"]({"DatabaseName": database})["TableList"]:
        params = table.get("Parameters") or {}
        if params.get("UPDATED_BY_CRAWLER") != crawler or table["Name"] in seen:
            continue
        if behavior == "DELETE_FROM_DATABASE":
            _ACTIONS["AWSGlue.DeleteTable"](
                {"DatabaseName": database, "Name": table["Name"]}
            )
        else:
            table_input = {
                k: table[k]
                for k in (
                    "Name",
                    "StorageDescriptor",
                    "PartitionKeys",
                    "TableType",
                    "Parameters",
                )
                if k in table
            }
            table_input["Parameters"] = {**params, "DEPRECATED_BY_CRAWLER": "1"}
            _ACTIONS["AWSGlue.UpdateTable"](
                {"DatabaseName": database, "TableInput": table_input}
            )
        gone += 1
    return gone


# ---------------------------------------------------------------------------
# Schedules: cron(...) crawlers start themselves, checked every few seconds
# ---------------------------------------------------------------------------
_scheduler_started = False


def start_scheduler() -> None:
    """Start the background loop that runs scheduled crawlers (once per process)."""
    global _scheduler_started
    with _lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    threading.Thread(target=_schedule_loop, daemon=True).start()


def due_crawlers(state: dict, now, fired: dict[str, float]) -> list[str]:
    """Return the READY crawlers whose cron schedule matches ``now`` (UTC), once a minute."""
    from oblako.engines.eventbridge.app import cron_matches

    minute = now.replace(second=0, microsecond=0).timestamp()
    due = []
    for name, crawler in state["crawlers"].items():
        schedule = crawler.get("Schedule") or {}
        if schedule.get("State") != "SCHEDULED" or crawler.get("State") != "READY":
            continue
        if (
            cron_matches(schedule.get("ScheduleExpression", ""), now)
            and fired.get(name) != minute
        ):
            fired[name] = minute
            due.append(name)
    return due


def _schedule_loop() -> None:
    """Start scheduled crawlers when their cron matches, every few seconds."""
    import datetime

    fired: dict[str, float] = {}
    while True:
        time.sleep(5)
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            for name in due_crawlers(_load(), now, fired):
                start(name)
        except Exception as e:  # keep the loop alive
            print(f"oblako glue: crawler schedule: {e!r}", flush=True)
