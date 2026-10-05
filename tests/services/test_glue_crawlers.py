"""Glue crawlers and classifiers over oblako's S3.

The Glue engine runs in-process with its own state; the files are real objects in
oblako's S3 (S3Proxy on 9000), so the test skips when S3 isn't running. Parquet
needs pyarrow.
"""

import io
import json
import time
import uuid

import pytest
from starlette.testclient import TestClient

import oblako.engines.glue_catalog as glue
from oblako import ports
from oblako.engines.glue_catalog import crawlers
from oblako.engines.glue_catalog.store import GlueStore

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


class _NoRest:
    """The Iceberg REST catalog, absent: these tests make Hive-style tables only."""

    def request(self, method, url, **kwargs):
        import httpx

        return httpx.Response(404, json={"error": {"message": "no REST catalog"}})


@pytest.fixture
def s3():
    from oblako.services import S3ProxyService

    client = S3ProxyService().get_client()
    try:
        client.list_buckets()
    except Exception as e:
        pytest.skip(f"oblako's S3 isn't running on {ports.S3}: {e}")
    return client


@pytest.fixture
def call(tmp_path, monkeypatch):
    monkeypatch.setattr(glue, "_stores", [GlueStore(":memory:")])
    monkeypatch.setattr(glue.httpx, "request", _NoRest().request)
    monkeypatch.setattr(crawlers, "STATE", tmp_path / "crawlers.json")
    client = TestClient(glue.create_app())

    def _call(action, body):
        resp = client.post(
            "/", content=json.dumps(body), headers={"X-Amz-Target": f"AWSGlue.{action}"}
        )
        return (
            resp.json()
            if resp.status_code == 200
            else (_ for _ in ()).throw(
                AssertionError(f"{action}: {resp.status_code} {resp.text}")
            )
        )

    return _call


def _parquet(table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def _wait(call, name):
    for _ in range(200):
        crawler = call("GetCrawler", {"Name": name})["Crawler"]
        if crawler["State"] == "READY" and "LastCrawl" in crawler:
            return crawler
        time.sleep(0.05)
    raise AssertionError("crawler never finished")


@pytest.fixture
def lake(s3):
    bucket, root = "glue-crawler-tests", f"{uuid.uuid4().hex[:8]}/"
    try:
        s3.create_bucket(Bucket=bucket)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    sales = pa.table({"id": pa.array([1, 2], pa.int64()), "amount": [1.5, 2.5]})
    for month in ("01", "02"):
        s3.put_object(
            Bucket=bucket,
            Key=f"{root}sales/year=2026/month={month}/part-0.parquet",
            Body=_parquet(sales),
        )
    s3.put_object(Bucket=bucket, Key=f"{root}sales/_SUCCESS", Body=b"")
    for region in ("eu", "us"):
        s3.put_object(
            Bucket=bucket,
            Key=f"{root}visits/{region}/v.csv",
            Body=b"page;hits;ok\n/;10;true\n/docs;3;false\n",
        )
    s3.put_object(
        Bucket=bucket,
        Key=f"{root}events/clicks/e.json",
        Body=b'{"user": "a", "n": 1}\n',
    )
    s3.put_object(
        Bucket=bucket,
        Key=f"{root}events/orders/o.json",
        Body=b'{"order": 7, "total": 9.5, "items": ["x"]}\n',
    )
    return s3, bucket, root


def test_classifiers_crud(call):
    call(
        "CreateClassifier",
        {
            "CsvClassifier": {
                "Name": "semi",
                "Delimiter": ";",
                "ContainsHeader": "PRESENT",
            }
        },
    )
    call("UpdateClassifier", {"CsvClassifier": {"Name": "semi", "QuoteSymbol": "'"}})
    got = call("GetClassifier", {"Name": "semi"})["Classifier"]["CsvClassifier"]
    assert (got["Delimiter"], got["QuoteSymbol"], got["Version"]) == (";", "'", 2)
    call("CreateClassifier", {"JsonClassifier": {"Name": "arr", "JsonPath": "$[*]"}})
    assert len(call("GetClassifiers", {})["Classifiers"]) == 2
    call("DeleteClassifier", {"Name": "arr"})
    assert len(call("GetClassifiers", {})["Classifiers"]) == 1


def test_crawler_builds_tables_partitions_and_schemas(call, lake):
    s3, bucket, root = lake
    call("CreateDatabase", {"DatabaseInput": {"Name": "lake"}})
    call(
        "CreateClassifier",
        {
            "CsvClassifier": {
                "Name": "semi",
                "Delimiter": ";",
                "ContainsHeader": "PRESENT",
            }
        },
    )
    call(
        "CreateCrawler",
        {
            "Name": "lake-crawler",
            "Role": "arn:aws:iam::123456789012:role/glue",
            "DatabaseName": "lake",
            "TablePrefix": "raw_",
            "Classifiers": ["semi"],
            "Targets": {"S3Targets": [{"Path": f"s3://{bucket}/{root}"}]},
        },
    )
    call("StartCrawler", {"Name": "lake-crawler"})
    crawler = _wait(call, "lake-crawler")
    assert crawler["LastCrawl"]["Status"] == "SUCCEEDED", crawler["LastCrawl"]
    tables = {
        t["Name"]: t for t in call("GetTables", {"DatabaseName": "lake"})["TableList"]
    }
    assert sorted(tables) == ["raw_clicks", "raw_orders", "raw_sales", "raw_visits"]

    sales = tables["raw_sales"]
    assert [c["Name"] for c in sales["StorageDescriptor"]["Columns"]] == [
        "id",
        "amount",
    ]
    assert [k["Name"] for k in sales["PartitionKeys"]] == ["year", "month"]
    assert sales["Parameters"]["classification"] == "parquet"
    parts = call("GetPartitions", {"DatabaseName": "lake", "TableName": "raw_sales"})
    assert sorted(p["Values"] for p in parts["Partitions"]) == [
        ["2026", "01"],
        ["2026", "02"],
    ]

    visits = tables["raw_visits"]  # same columns in each folder: one table
    assert [k["Name"] for k in visits["PartitionKeys"]] == ["partition_0"]
    types = {c["Name"]: c["Type"] for c in visits["StorageDescriptor"]["Columns"]}
    assert types == {"page": "string", "hits": "bigint", "ok": "boolean"}
    assert visits["Parameters"]["delimiter"] == ";"

    orders = tables["raw_orders"]  # different JSON schemas: a table each
    assert {c["Name"]: c["Type"] for c in orders["StorageDescriptor"]["Columns"]} == {
        "order": "bigint",
        "total": "double",
        "items": "array<string>",
    }
    metrics = call("GetCrawlerMetrics", {"CrawlerNameList": ["lake-crawler"]})
    assert metrics["CrawlerMetricsList"][0]["TablesCreated"] == 4

    # a second crawl updates; a dataset that's gone deprecates its table
    for key in ("visits/eu/v.csv", "visits/us/v.csv"):
        s3.delete_object(Bucket=bucket, Key=root + key)
    call("StartCrawler", {"Name": "lake-crawler"})
    time.sleep(0.1)
    _wait(call, "lake-crawler")
    metrics = call("GetCrawlerMetrics", {"CrawlerNameList": ["lake-crawler"]})[
        "CrawlerMetricsList"
    ][0]
    assert (metrics["TablesCreated"], metrics["TablesDeleted"]) == (0, 1)
    gone = call("GetTable", {"DatabaseName": "lake", "Name": "raw_visits"})["Table"]
    assert gone["Parameters"]["DEPRECATED_BY_CRAWLER"] == "1"


def test_crawler_errors(call):
    call(
        "CreateCrawler",
        {
            "Name": "c",
            "Role": "r",
            "DatabaseName": "nope",
            "Targets": {"S3Targets": []},
        },
    )
    call("StartCrawler", {"Name": "c"})
    crawler = _wait(call, "c")
    assert crawler["LastCrawl"]["Status"] == "FAILED"
    assert "nope" in crawler["LastCrawl"]["ErrorMessage"]


def test_scheduled_crawlers_are_due_once_a_minute():
    import datetime

    state = {
        "crawlers": {
            "nightly": {
                "State": "READY",
                "Schedule": {
                    "ScheduleExpression": "cron(0 2 * * ? *)",
                    "State": "SCHEDULED",
                },
            },
            "paused": {
                "State": "READY",
                "Schedule": {
                    "ScheduleExpression": "cron(0 2 * * ? *)",
                    "State": "NOT_SCHEDULED",
                },
            },
        }
    }
    two = datetime.datetime(2026, 10, 5, 2, 0, 30, tzinfo=datetime.timezone.utc)
    fired: dict = {}
    assert crawlers.due_crawlers(state, two, fired) == ["nightly"]
    assert crawlers.due_crawlers(state, two, fired) == []
