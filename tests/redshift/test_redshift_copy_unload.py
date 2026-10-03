"""Integration tests for the in-image Redshift COPY/UNLOAD <-> S3 bridge.

Requires running services:
    docker compose up -d redshift s3proxy   # engine on 5439, S3Proxy on 9000

The wire proxy rewrites Redshift ``COPY t FROM 's3://...'`` and
``UNLOAD ('q') TO 's3://...'`` into ``oblako_*`` plpython3u functions that move
Parquet to/from S3, so every wire client gets working COPY/UNLOAD. Exercised here
over both the simple-query path (psycopg2) and the extended protocol
(redshift_connector, awswrangler's driver), plus awswrangler itself when present.

Endpoints default to the canonical ports but can be pointed elsewhere with
OBLAKO_TEST_RS_PORT / OBLAKO_TEST_S3_ENDPOINT (used to run against an isolated
stack without touching a warehouse already on 5439).
"""

import gzip
import json
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
BUCKET = "rs-bridge-test"


def _bridge_present() -> bool:
    """True if the engine has the COPY/UNLOAD bridge (the oblako_* UDFs exist)."""
    try:
        c = psycopg2.connect(connect_timeout=3, **RS)
        c.autocommit = True
        try:
            cur = c.cursor()
            cur.execute(
                "SELECT count(*) FROM pg_proc WHERE proname = 'oblako_unload_to_s3'"
            )
            return cur.fetchone()[0] > 0
        finally:
            c.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _bridge_present(), reason="redshift image without the COPY/UNLOAD bridge"
)


@pytest.fixture
def s3():
    client = boto3.client(
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
        client.create_bucket(Bucket=BUCKET)
    except client.exceptions.ClientError:
        pass
    return client


@pytest.fixture
def conn():
    c = psycopg2.connect(**RS)
    c.autocommit = True
    yield c
    c.close()


def test_unload_then_copy_roundtrip(conn, s3):
    """UNLOAD writes real Parquet to S3; COPY reads it back (simple-query path)."""
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS cu_src")
    cur.execute(
        "CREATE TABLE cu_src (id int, name varchar(16), amount float8, ts timestamptz)"
    )
    cur.execute(
        "INSERT INTO cu_src VALUES "
        "(1,'alice',1.5,'2021-04-12 10:00:00+00'),(2,'bob',2.5,'2021-04-12 11:00:00+00')"
    )
    cur.execute(
        "UNLOAD ('SELECT * FROM cu_src ORDER BY id') TO 's3://rs-bridge-test/dump/' "
        "IAM_ROLE 'arn:x' FORMAT AS PARQUET"
    )
    objs = s3.list_objects_v2(Bucket=BUCKET, Prefix="dump/").get("Contents", [])
    assert objs, "UNLOAD wrote no S3 objects"
    body = s3.get_object(Bucket=BUCKET, Key=objs[0]["Key"])["Body"].read()
    assert body[:4] == b"PAR1"  # real Parquet

    cur.execute("DROP TABLE IF EXISTS cu_dst")
    cur.execute(
        "CREATE TABLE cu_dst (id int, name varchar(16), amount float8, ts timestamptz)"
    )
    cur.execute(
        "COPY cu_dst FROM 's3://rs-bridge-test/dump/' IAM_ROLE 'arn:x' FORMAT AS PARQUET"
    )
    cur.execute("SELECT id, name, amount FROM cu_dst ORDER BY id")
    assert cur.fetchall() == [(1, "alice", 1.5), (2, "bob", 2.5)]
    cur.execute("DROP TABLE cu_src")
    cur.execute("DROP TABLE cu_dst")


def test_extended_protocol_via_redshift_connector(s3):
    """The same round-trip over the extended protocol (redshift_connector)."""
    rc = pytest.importorskip("redshift_connector")
    con = rc.connect(
        host="localhost",
        port=RS_PORT,
        database="oblako",
        user="oblako",
        password="oblako",
        ssl=True,
        sslmode="verify-ca",  # oblako trust has added the cert
    )
    con.autocommit = True
    try:
        cur = con.cursor()
        cur.execute("DROP TABLE IF EXISTS cu_ext")
        cur.execute("CREATE TABLE cu_ext (id int, k int)")
        cur.execute("INSERT INTO cu_ext VALUES (1,10),(2,20)")
        cur.execute(
            "UNLOAD ('SELECT * FROM cu_ext ORDER BY id') TO 's3://rs-bridge-test/ext/' "
            "IAM_ROLE 'x' FORMAT AS PARQUET"
        )
        cur.execute("DROP TABLE IF EXISTS cu_ext2")
        cur.execute("CREATE TABLE cu_ext2 (id int, k int)")
        cur.execute(
            "COPY cu_ext2 FROM 's3://rs-bridge-test/ext/' IAM_ROLE 'x' FORMAT AS PARQUET"
        )
        cur.execute("SELECT id, k FROM cu_ext2 ORDER BY id")
        assert [tuple(r) for r in cur.fetchall()] == [(1, 10), (2, 20)]
    finally:
        con.close()


def test_temp_table_batch_unload(conn, s3):
    """CREATE TEMP TABLE ...; UNLOAD(...) as one batch: UNLOAD sees the temp table.

    This is the redshift-data engine's exact pattern (Feast's retrieval), and it
    works because the rewritten oblako_unload_to_s3 runs in the same session.
    """
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS cu_base")
    cur.execute("CREATE TABLE cu_base (id int, v int)")
    cur.execute("INSERT INTO cu_base VALUES (1,100),(2,200)")
    cur.execute(
        "CREATE TEMPORARY TABLE _cu_t AS (SELECT * FROM cu_base); "
        "UNLOAD ('SELECT * FROM _cu_t') TO 's3://rs-bridge-test/tmp/' "
        "IAM_ROLE 'x' FORMAT AS PARQUET"
    )
    objs = s3.list_objects_v2(Bucket=BUCKET, Prefix="tmp/").get("Contents", [])
    assert objs, "temp-table UNLOAD wrote no S3 objects"
    cur.execute("DROP TABLE cu_base")


def test_csv_unload_and_copy_roundtrip(conn, s3):
    """UNLOAD to CSV (with a header) writes real CSV; COPY reads it back."""
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS cu_csv_src")
    cur.execute("CREATE TABLE cu_csv_src (id int, name varchar(16), amount float8)")
    cur.execute("INSERT INTO cu_csv_src VALUES (1,'alice',1.5),(2,'bob',2.5)")
    cur.execute(
        "UNLOAD ('SELECT * FROM cu_csv_src ORDER BY id') TO 's3://rs-bridge-test/csv/' "
        "IAM_ROLE 'x' FORMAT AS CSV HEADER"
    )
    objs = s3.list_objects_v2(Bucket=BUCKET, Prefix="csv/").get("Contents", [])
    assert objs, "CSV UNLOAD wrote no S3 objects"
    text = s3.get_object(Bucket=BUCKET, Key=objs[0]["Key"])["Body"].read().decode()
    lines = text.splitlines()
    assert lines[0] == "id,name,amount"  # HEADER row
    assert lines[1] == "1,alice,1.5"

    cur.execute("DROP TABLE IF EXISTS cu_csv_dst")
    cur.execute("CREATE TABLE cu_csv_dst (id int, name varchar(16), amount float8)")
    cur.execute(
        "COPY cu_csv_dst FROM 's3://rs-bridge-test/csv/' IAM_ROLE 'x' "
        "FORMAT AS CSV IGNOREHEADER 1"
    )
    cur.execute("SELECT id, name, amount FROM cu_csv_dst ORDER BY id")
    assert cur.fetchall() == [(1, "alice", 1.5), (2, "bob", 2.5)]
    cur.execute("DROP TABLE cu_csv_src")
    cur.execute("DROP TABLE cu_csv_dst")


def test_unsupported_format_is_rejected(conn):
    """An unsupported binary format (AVRO) fails with a clear message."""
    cur = conn.cursor()
    with pytest.raises(psycopg2.Error) as excinfo:
        cur.execute(
            "COPY cu_x FROM 's3://rs-bridge-test/x' IAM_ROLE 'x' FORMAT AS AVRO"
        )
    assert "unsupported" in str(excinfo.value).lower()


def test_awswrangler_copy_and_unload(s3, monkeypatch, tmp_path):
    """awswrangler's wr.redshift.copy / unload work end to end against the bridge."""
    wr = pytest.importorskip("awswrangler")
    rc = pytest.importorskip("redshift_connector")
    import pandas as pd

    # awswrangler builds its own boto3 clients, so steer them at S3Proxy via env:
    # endpoint, dummy creds, path-style addressing, and checksums only when required.
    cfg = tmp_path / "awscfg"
    cfg.write_text("[default]\nregion = us-east-1\ns3 =\n    addressing_style = path\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", S3_ENDPOINT)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")

    con = rc.connect(
        host="localhost",
        port=RS_PORT,
        database="oblako",
        user="oblako",
        password="oblako",
        ssl=True,
        sslmode="verify-ca",  # oblako trust has added the cert
    )
    try:
        df = pd.DataFrame({"id": [10, 20, 30], "name": ["x", "y", "z"]})
        wr.redshift.copy(
            df=df,
            path="s3://rs-bridge-test/wr/",
            con=con,
            schema="public",
            table="wr_target",
            mode="overwrite",
            iam_role="arn:aws:iam::0:role/x",
        )
        out = wr.redshift.unload(
            sql="SELECT * FROM wr_target ORDER BY id",
            path="s3://rs-bridge-test/wru/",
            con=con,
            iam_role="arn:aws:iam::0:role/x",
        )
        assert list(out["id"]) == [10, 20, 30]
        assert list(out["name"]) == ["x", "y", "z"]
    finally:
        con.close()


def _super(value):
    """A SUPER (jsonb-domain) column comes back as JSON text via psycopg2."""
    return value if isinstance(value, (list, dict)) else json.loads(value)


def test_json_copy_auto_shreds_columns_and_super(conn, s3):
    """FORMAT JSON 'auto' matches keys to columns; nested values land in SUPER."""
    body = (
        b'{"r_regionkey":0,"r_name":"AFRICA","r_nations":[{"n":"AF"},{"n":"EG"}]}\n'
        b'{"r_regionkey":1,"r_name":"AMERICA","r_nations":[]}'
    )
    s3.put_object(Bucket=BUCKET, Key="json_auto/data.json", Body=body)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS cu_json_auto")
    cur.execute(
        "CREATE TABLE cu_json_auto (r_regionkey smallint, r_name varchar, r_nations super)"
    )
    cur.execute(
        "COPY cu_json_auto FROM 's3://rs-bridge-test/json_auto/' "
        "IAM_ROLE 'x' FORMAT AS JSON 'auto'"
    )
    cur.execute(
        "SELECT r_regionkey, r_name, r_nations FROM cu_json_auto ORDER BY r_regionkey"
    )
    rows = cur.fetchall()
    assert [(r[0], r[1]) for r in rows] == [(0, "AFRICA"), (1, "AMERICA")]
    assert _super(rows[0][2]) == [{"n": "AF"}, {"n": "EG"}]
    assert _super(rows[1][2]) == []
    cur.execute("DROP TABLE cu_json_auto")


def test_json_copy_auto_ignorecase(conn, s3):
    """'auto ignorecase' matches mixed-case JSON keys to lower-case columns."""
    s3.put_object(Bucket=BUCKET, Key="json_ic/d.json", Body=b'{"Id":5,"Name":"x"}')
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS cu_json_ic")
    cur.execute("CREATE TABLE cu_json_ic (id int, name varchar)")
    cur.execute(
        "COPY cu_json_ic FROM 's3://rs-bridge-test/json_ic/' "
        "IAM_ROLE 'x' JSON 'auto ignorecase'"
    )
    cur.execute("SELECT id, name FROM cu_json_ic")
    assert cur.fetchall() == [(5, "x")]
    cur.execute("DROP TABLE cu_json_ic")


def test_json_copy_noshred_into_super(conn, s3):
    """'noshred' loads the whole JSON document into a single SUPER column."""
    s3.put_object(Bucket=BUCKET, Key="json_ns/d.json", Body=b'{"a":1,"b":{"c":2}}')
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS cu_json_ns")
    cur.execute("CREATE TABLE cu_json_ns (rdata super)")
    cur.execute(
        "COPY cu_json_ns FROM 's3://rs-bridge-test/json_ns/' IAM_ROLE 'x' JSON 'noshred'"
    )
    cur.execute("SELECT rdata FROM cu_json_ns")
    assert _super(cur.fetchone()[0]) == {"a": 1, "b": {"c": 2}}
    cur.execute("DROP TABLE cu_json_ns")


def test_json_copy_jsonpaths(conn, s3):
    """A jsonpaths file maps JSON paths to columns positionally."""
    s3.put_object(
        Bucket=BUCKET, Key="json_jp/d.json", Body=b'{"rk":0,"nm":"AF","meta":{"x":1}}'
    )
    s3.put_object(
        Bucket=BUCKET,
        Key="paths/np.json",
        Body=b'{"jsonpaths":["$.rk","$.nm","$.meta"]}',
    )
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS cu_json_jp")
    cur.execute(
        "CREATE TABLE cu_json_jp (regionkey smallint, name varchar, meta super)"
    )
    cur.execute(
        "COPY cu_json_jp FROM 's3://rs-bridge-test/json_jp/' "
        "IAM_ROLE 'x' FORMAT AS JSON 's3://rs-bridge-test/paths/np.json'"
    )
    cur.execute("SELECT regionkey, name, meta FROM cu_json_jp")
    row = cur.fetchone()
    assert (row[0], row[1]) == (0, "AF")
    assert _super(row[2]) == {"x": 1}
    cur.execute("DROP TABLE cu_json_jp")


def test_json_copy_gzip(conn, s3):
    """GZIP-compressed JSON is decompressed and loaded."""
    s3.put_object(
        Bucket=BUCKET, Key="json_gz/d.json.gz", Body=gzip.compress(b'{"a":7,"b":8}')
    )
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS cu_json_gz")
    cur.execute("CREATE TABLE cu_json_gz (a int, b int)")
    cur.execute(
        "COPY cu_json_gz FROM 's3://rs-bridge-test/json_gz/' "
        "IAM_ROLE 'x' FORMAT AS JSON 'auto' GZIP"
    )
    cur.execute("SELECT a, b FROM cu_json_gz")
    assert cur.fetchall() == [(7, 8)]
    cur.execute("DROP TABLE cu_json_gz")
