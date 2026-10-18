"""Glue over JDBC: an incremental sync from RDS PostgreSQL into another Postgres.

The pattern of AWS's "sync RDS PostgreSQL to Supabase with Glue": Glue connections
to both databases, a crawler that catalogs the source's tables over JDBC, and a
job that reads a crawled table through its connection with job bookmarks
(``jobBookmarkKeys`` on ``updated_at``) and writes what's new through the
target's connection. A second run after new rows copies only those, a run with
nothing new copies nothing, and ``ResetJobBookmark`` makes the next run read
everything again. Needs Docker, the ~5 GB Glue image and ``postgres:16``; skips
otherwise (CI doesn't pull the image).
"""

import socket
import threading
import time

import httpx
import pytest

from oblako.services.glue import IMAGE_TAG, GlueService

psycopg = pytest.importorskip("psycopg")

SCRIPT = """
import sys
from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext

args = getResolvedOptions(sys.argv, ["JOB_NAME", "url"])
glue = GlueContext(SparkContext.getOrCreate())
job = Job(glue)
job.init(args["JOB_NAME"], args)
db = {"url": args["url"], "user": "postgres", "password": "oblako"}
rows = glue.create_dynamic_frame.from_options(
    connection_type="postgresql",
    connection_options={**db, "dbtable": "customers", "jobBookmarkKeys": ["updated_at"]},
    transformation_ctx="customers",
)
glue.write_dynamic_frame.from_options(
    frame=rows.select_fields(["id"]),
    connection_type="postgresql",
    connection_options={**db, "dbtable": "copied"},
)
job.commit()
"""


def _ready() -> bool:
    try:
        client = GlueService().client
        client.images.get(IMAGE_TAG)
        client.images.get("postgres:16")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _ready(), reason="needs Docker, the Glue 5 image and postgres:16"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A Glue engine of its own, with its job state in a temporary directory."""
    import uvicorn

    from oblako.engines import glue_catalog
    from oblako.engines.glue_catalog import jobs

    from oblako.engines.glue_catalog import crawlers
    from oblako.engines.glue_catalog.store import GlueStore

    monkeypatch.setattr(jobs, "STATE", tmp_path / "jobs.json")
    monkeypatch.setattr(crawlers, "STATE", tmp_path / "crawlers.json")
    monkeypatch.setattr(glue_catalog, "_stores", [GlueStore(str(tmp_path / "glue.db"))])
    monkeypatch.setenv("OBLAKO_ICEBERG_URL", "http://127.0.0.1:9")
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(glue_catalog.create_app(), port=port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    def call(action, body):
        return httpx.post(
            f"http://127.0.0.1:{port}/",
            json=body,
            headers={"X-Amz-Target": f"AWSGlue.{action}"},
        ).json()

    call.port = port
    yield call
    server.should_exit = True


@pytest.fixture
def source():
    """A Postgres the job reaches at host.docker.internal; yields (url, connection)."""
    client = GlueService().client
    container = client.containers.run(
        "postgres:16",
        detach=True,
        environment={"POSTGRES_PASSWORD": "oblako"},
        ports={"5432/tcp": ("127.0.0.1", None)},
    )
    try:
        container.reload()
        port = int(container.ports["5432/tcp"][0]["HostPort"])
        for _ in range(120):
            try:
                conn = psycopg.connect(
                    host="127.0.0.1",
                    port=port,
                    user="postgres",
                    password="oblako",
                    autocommit=True,
                )
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        conn.execute("CREATE TABLE customers (id int, updated_at timestamp)")
        conn.execute("CREATE TABLE copied (id int)")
        conn.execute("CREATE DATABASE supabase")
        yield f"jdbc:postgresql://host.docker.internal:{port}/", conn
        conn.close()
    finally:
        container.remove(force=True)


def _run(engine, url: str, script: str = SCRIPT) -> None:
    from oblako.engines.glue_catalog import jobs

    out = GlueService().submit_job(
        script,
        args=jobs._argv(
            "sync",
            f"jr_{time.time_ns()}",
            {"--job-bookmark-option": "job-bookmark-enable", "--url": url},
        ),
        env={"AWS_ENDPOINT_URL_GLUE": f"http://host.docker.internal:{engine.port}"},
    )
    assert out["exit_code"] == 0, out["logs"][-3000:]


def test_each_run_copies_only_the_rows_past_the_bookmark(engine, source):
    url, db = source
    engine(
        "CreateJob",
        {
            "Name": "sync",
            "Role": "arn:aws:iam::123456789012:role/glue",
            "Command": {"Name": "glueetl", "ScriptLocation": "s3://b/sync.py"},
        },
    )

    def copied():
        return sorted(r[0] for r in db.execute("SELECT id FROM copied").fetchall())

    db.execute(
        "INSERT INTO customers VALUES (1, '2026-10-01 10:00'), "
        "(2, '2026-10-01 11:00'), (3, '2026-10-01 11:00')"
    )
    _run(engine, url + "postgres")
    assert copied() == [1, 2, 3]
    db.execute(
        "INSERT INTO customers VALUES (4, '2026-10-02 09:00'), (5, '2026-10-02 09:30')"
    )
    db.execute("UPDATE customers SET updated_at = '2026-10-02 10:00' WHERE id = 1")
    _run(engine, url + "postgres")
    assert copied() == [1, 1, 2, 3, 4, 5]
    _run(engine, url + "postgres")
    assert copied() == [1, 1, 2, 3, 4, 5]
    entry = engine("GetJobBookmark", {"JobName": "sync"})["JobBookmarkEntry"]
    assert entry["Run"] == 2 and "2026-10-02T10:00:00" in entry["JobBookmark"]
    engine("ResetJobBookmark", {"JobName": "sync"})
    _run(engine, url + "postgres")
    assert copied() == [1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]


SYNC = """
import sys
from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext

args = getResolvedOptions(sys.argv, ["JOB_NAME"])
glue = GlueContext(SparkContext.getOrCreate())
job = Job(glue)
job.init(args["JOB_NAME"], args)
rows = glue.create_dynamic_frame.from_catalog(
    database="rds",
    table_name="postgres_public_customers",
    additional_options={"jobBookmarkKeys": ["updated_at"]},
    transformation_ctx="customers",
)
glue.write_dynamic_frame.from_jdbc_conf(
    frame=rows,
    catalog_connection="supabase",
    connection_options={"dbtable": "customers_stage", "database": "supabase"},
)
job.commit()
"""


def _wait_crawl(engine, name: str) -> dict:
    for _ in range(200):
        crawler = engine("GetCrawler", {"Name": name})["Crawler"]
        if crawler["State"] == "READY" and "LastCrawl" in crawler:
            return crawler["LastCrawl"]
        time.sleep(0.1)
    raise TimeoutError(name)


def test_crawled_rds_tables_sync_through_named_connections(engine, source):
    url, db = source
    for name, database in (("rds", "postgres"), ("supabase", "supabase")):
        engine(
            "CreateConnection",
            {
                "ConnectionInput": {
                    "Name": name,
                    "ConnectionType": "JDBC",
                    "ConnectionProperties": {
                        "JDBC_CONNECTION_URL": url + database,
                        "USERNAME": "postgres",
                        "PASSWORD": "oblako",
                    },
                }
            },
        )
    db.execute(
        "CREATE TABLE amounts (id bigint, total numeric(12,2), paid boolean, "
        "on_day date, note text)"
    )
    db.execute("CREATE VIEW recent AS SELECT id FROM customers")
    engine("CreateDatabase", {"DatabaseInput": {"Name": "rds"}})
    engine(
        "CreateCrawler",
        {
            "Name": "rds",
            "Role": "arn:aws:iam::123456789012:role/glue",
            "DatabaseName": "rds",
            "Targets": {
                "JdbcTargets": [
                    {
                        "ConnectionName": "rds",
                        "Path": "postgres/public/%",
                        "Exclusions": ["postgres/public/copied"],
                    }
                ]
            },
        },
    )
    engine("StartCrawler", {"Name": "rds"})
    assert _wait_crawl(engine, "rds")["Status"] == "SUCCEEDED"
    tables = {
        t["Name"]: t for t in engine("GetTables", {"DatabaseName": "rds"})["TableList"]
    }
    assert sorted(tables) == [
        "postgres_public_amounts",
        "postgres_public_customers",
        "postgres_public_recent",
    ]
    amounts = tables["postgres_public_amounts"]
    assert [
        (c["Name"], c["Type"]) for c in amounts["StorageDescriptor"]["Columns"]
    ] == [
        ("id", "bigint"),
        ("total", "decimal(12,2)"),
        ("paid", "boolean"),
        ("on_day", "date"),
        ("note", "string"),
    ]
    assert amounts["StorageDescriptor"]["Location"] == "postgres.public.amounts"
    assert amounts["Parameters"]["classification"] == "postgresql"
    assert amounts["Parameters"]["connectionName"] == "rds"
    assert tables["postgres_public_recent"]["Parameters"]["typeOfData"] == "view"

    with psycopg.connect(
        host="127.0.0.1",
        port=int(url.rsplit(":", 1)[1].strip("/")),
        dbname="supabase",
        user="postgres",
        password="oblako",
        autocommit=True,
    ) as target:
        target.execute("CREATE TABLE customers_stage (id int, updated_at timestamp)")

        def staged():
            return sorted(
                r[0]
                for r in target.execute("SELECT id FROM customers_stage").fetchall()
            )

        engine(
            "CreateJob",
            {
                "Name": "sync",
                "Role": "arn:aws:iam::123456789012:role/glue",
                "Command": {"Name": "glueetl", "ScriptLocation": "s3://b/sync.py"},
            },
        )
        db.execute(
            "INSERT INTO customers VALUES (1, '2026-10-01 10:00'), (2, '2026-10-01 11:00')"
        )
        _run(engine, url, SYNC)
        assert staged() == [1, 2]
        db.execute("INSERT INTO customers VALUES (3, '2026-10-02 09:00')")
        _run(engine, url, SYNC)
        assert staged() == [1, 2, 3]
