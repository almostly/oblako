"""Integration tests for the Redshift SUPER type + PartiQL navigation.

Requires running services:
    docker compose up -d redshift s3proxy   # engine on 5439, S3Proxy on 9000

SUPER is a domain over jsonb; the wire proxy rewrites PartiQL dot-navigation
(``data.a.b``) into a jsonb path, while bracket navigation (``data['a'][0]``) runs
natively. Nested Parquet loaded via COPY lands in a SUPER column as JSON.

Endpoints default to the canonical ports; override with OBLAKO_TEST_RS_PORT /
OBLAKO_TEST_S3_ENDPOINT to run against an isolated stack.
"""

import io
import os

import boto3
import psycopg2
import pytest
from botocore.config import Config

RS_PORT = int(os.environ.get("OBLAKO_TEST_RS_PORT", "5439"))
S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
RS = dict(
    host="localhost", port=RS_PORT, user="oblako", password="oblako", dbname="oblako"
)
BUCKET = "rs-super-test"


def _super_present() -> bool:
    """True if the engine has the SUPER type (the domain over jsonb)."""
    try:
        c = psycopg2.connect(connect_timeout=3, **RS)
        c.autocommit = True
        try:
            cur = c.cursor()
            cur.execute("SELECT to_regtype('super')")
            return cur.fetchone()[0] is not None
        finally:
            c.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _super_present(), reason="redshift image without the SUPER type"
)


@pytest.fixture
def conn():
    c = psycopg2.connect(**RS)
    c.autocommit = True
    yield c
    c.close()


def test_super_dot_and_bracket_navigation(conn):
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS sup_events")
    cur.execute("CREATE TABLE sup_events (id int, data SUPER)")  # proxy learns 'data'
    cur.execute(
        """INSERT INTO sup_events VALUES
        (1, JSON_PARSE('{"type":"premium","customer":{"name":"Alice"},"tags":["a","b"],"age":30}')),
        (2, JSON_PARSE('{"type":"basic","customer":{"name":"Bob"},"tags":["c"],"age":25}'))"""
    )
    # dot navigation (rewritten by the proxy)
    cur.execute("SELECT id, data.customer.name FROM sup_events ORDER BY id")
    assert cur.fetchall() == [(1, "Alice"), (2, "Bob")]
    cur.execute("SELECT id FROM sup_events WHERE data.type = 'premium'")
    assert cur.fetchall() == [(1,)]
    cur.execute("SELECT data.tags[0] FROM sup_events ORDER BY id")
    assert [r[0] for r in cur.fetchall()] == ["a", "c"]
    # a numeric comparison needs an explicit cast (documented)
    cur.execute("SELECT id FROM sup_events WHERE (data.age)::int > 27")
    assert cur.fetchall() == [(1,)]
    # bracket navigation runs natively (jsonb subscripting)
    cur.execute("SELECT data['customer']['name'] FROM sup_events WHERE id = 1")
    assert cur.fetchone()[0] == "Alice"
    cur.execute("DROP TABLE sup_events")


def test_super_functions(conn):
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS sup_fn")
    cur.execute("CREATE TABLE sup_fn (id int, data SUPER)")
    cur.execute(
        'INSERT INTO sup_fn VALUES (1, JSON_PARSE(\'{"a":{"b":5},"arr":[1,2]}\'))'
    )
    cur.execute("SELECT json_typeof(data), json_typeof(data.arr) FROM sup_fn")
    assert cur.fetchone() == ("object", "array")
    cur.execute("SELECT json_serialize(data.a) FROM sup_fn")
    assert cur.fetchone()[0] == '{"b": 5}'
    cur.execute("SELECT is_valid_json('{\"x\":1}'), is_valid_json('nope')")
    assert cur.fetchone() == (True, False)
    cur.execute("DROP TABLE sup_fn")


def test_super_nested_parquet_copy(conn):
    """A Parquet struct column loads into a SUPER column as JSON (via COPY)."""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        ),
    )
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass

    table = pa.table(
        {
            "id": [1, 2],
            "doc": [
                {"name": "Alice", "roles": ["admin"]},
                {"name": "Bob", "roles": []},
            ],
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    s3.put_object(Bucket=BUCKET, Key="nested/d.parquet", Body=buf.getvalue())

    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS sup_copy")
    cur.execute("CREATE TABLE sup_copy (id int, doc SUPER)")
    cur.execute(
        f"COPY sup_copy FROM 's3://{BUCKET}/nested/d.parquet' "
        "IAM_ROLE 'x' FORMAT AS PARQUET"
    )
    # the struct landed as SUPER and is navigable
    cur.execute("SELECT id, doc.name, doc.roles[0] FROM sup_copy ORDER BY id")
    assert cur.fetchall() == [(1, "Alice", "admin"), (2, "Bob", None)]
    cur.execute("DROP TABLE sup_copy")
