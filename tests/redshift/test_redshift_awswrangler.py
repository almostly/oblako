"""Integration tests: awswrangler's redshift module against redshift-local.

Every function in ``wr.redshift``: the three ways to connect (a Secrets Manager
secret, a Glue connection, ``connect_temp``'s temporary credentials), reads,
``to_sql`` in each mode and with Redshift's table options, and COPY / UNLOAD
through S3. Needs the Redshift engine, S3, moto (Secrets Manager), the Glue
catalog engine and the Redshift API on their default ports; skips otherwise.
"""

import json
import uuid

import pytest

from oblako import ports

wr = pytest.importorskip("awswrangler")
pd = pytest.importorskip("pandas")
redshift_connector = pytest.importorskip("redshift_connector")
boto3 = pytest.importorskip("boto3")

U = uuid.uuid4().hex[:6]
BUCKET = "wr-redshift-tests"
ENDPOINTS = {
    "AWS_ENDPOINT_URL_S3": f"http://localhost:{ports.S3}",
    "AWS_ENDPOINT_URL_SECRETS_MANAGER": f"http://localhost:{ports.MOTO}",
    "AWS_ENDPOINT_URL_GLUE": f"http://localhost:{ports.GLUE_CATALOG}",
    "AWS_ENDPOINT_URL_REDSHIFT": f"http://localhost:{ports.REDSHIFT_CONTROL}",
}


@pytest.fixture(scope="module")
def aws():
    """Point awswrangler's boto3 clients at oblako, as AWS_PROFILE=oblako does."""
    mp = pytest.MonkeyPatch()
    for name, url in ENDPOINTS.items():
        mp.setenv(name, url)
    mp.setenv("AWS_ACCESS_KEY_ID", "oblako")
    mp.setenv("AWS_SECRET_ACCESS_KEY", "oblako")
    mp.setenv("AWS_DEFAULT_REGION", "us-east-1")
    mp.setenv("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    mp.delenv("AWS_PROFILE", raising=False)
    try:
        boto3.client("s3").list_buckets()
        boto3.client("secretsmanager").list_secrets()
        boto3.client("glue").get_databases()
        boto3.client("redshift").describe_clusters()
    except Exception as e:  # an engine isn't running
        mp.undo()
        pytest.skip(f"oblako's S3 / moto / Glue / Redshift API isn't running: {e}")
    s3 = boto3.client("s3")
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    yield
    mp.undo()


@pytest.fixture(scope="module")
def con(aws):
    try:
        conn = redshift_connector.connect(
            host="localhost",
            port=5439,
            database="oblako",
            user="oblako",
            password="oblako",
            ssl=True,
            sslmode="verify-ca",
        )
    except Exception as e:
        pytest.skip(f"redshift-local isn't reachable with verify-ca: {e}")
    yield conn
    conn.close()


@pytest.fixture
def df():
    return pd.DataFrame(
        {
            "id": [1, 2, 3],
            "name": ["a", "b", "c"],
            "amount": [1.5, 2.5, 3.5],
            "ts": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
            "flag": [True, False, True],
        }
    )


def _one(conn) -> list:
    cur = conn.cursor()
    cur.execute("SELECT current_user, session_user")
    return list(cur.fetchone())


def test_connect_with_a_secret(aws):
    secret = f"wr-{U}"
    boto3.client("secretsmanager").create_secret(
        Name=secret,
        SecretString=json.dumps(
            {
                "host": "localhost",
                "port": 5439,
                "username": "oblako",
                "password": "oblako",
                "engine": "redshift",
                "dbname": "oblako",
            }
        ),
    )
    assert (
        _one(wr.redshift.connect(secret_id=secret, sslmode="verify-ca"))[0] == "oblako"
    )


def test_connect_with_a_glue_connection(aws):
    name = f"wr-{U}"
    boto3.client("glue").create_connection(
        ConnectionInput={
            "Name": name,
            "ConnectionType": "JDBC",
            "ConnectionProperties": {
                "JDBC_CONNECTION_URL": "jdbc:redshift://localhost:5439/oblako",
                "USERNAME": "oblako",
                "PASSWORD": "oblako",
            },
        }
    )
    assert (
        _one(wr.redshift.connect(connection=name, sslmode="verify-ca"))[0] == "oblako"
    )


def test_connect_temp_issues_credentials_on_the_engine(aws):
    """GetClusterCredentials: an IAMA:<user> login that acts as <user>."""
    api = boto3.client("redshift")
    try:
        api.create_cluster(
            ClusterIdentifier="wr-temp",
            NodeType="ra3.large",
            ClusterType="single-node",
            MasterUsername="oblako",
            MasterUserPassword="Oblako123x",
            DBName="oblako",
        )
    except api.exceptions.ClusterAlreadyExistsFault:
        pass
    conn = wr.redshift.connect_temp(
        cluster_identifier="wr-temp",
        user="oblako",
        database="oblako",
        sslmode="verify-ca",
    )
    assert _one(conn) == ["oblako", "IAMA:oblako"]
    analyst = f"analyst_{U}"
    conn = wr.redshift.connect_temp(
        cluster_identifier="wr-temp",
        user=analyst,
        database="oblako",
        auto_create=True,
        sslmode="verify-ca",
    )
    assert _one(conn) == [analyst, f"IAMA:{analyst}"]


def test_to_sql_modes_and_table_options(con, df):
    t = f"wr_{U}"
    wr.redshift.to_sql(df, con, table=t, schema="public")
    assert wr.redshift.read_sql_table(t, con, schema="public").shape == (3, 5)
    for method in ("drop", "cascade", "truncate", "delete"):
        wr.redshift.to_sql(
            df, con, table=t, schema="public", mode="overwrite", overwrite_method=method
        )
    wr.redshift.to_sql(
        df,
        con,
        table=t + "_k",
        schema="public",
        mode="overwrite",
        diststyle="KEY",
        distkey="id",
        sortstyle="INTERLEAVED",
        sortkey=["id", "ts"],
        primary_keys=["id"],
        varchar_lengths={"name": 10},
        dtype={"amount": "DECIMAL(10,2)"},
    )
    update = pd.DataFrame(
        {
            "id": [2, 4],
            "name": ["B", "d"],
            "amount": [9.0, 4.5],
            "ts": pd.to_datetime(["2026-02-02", "2026-02-04"]),
            "flag": [True, True],
        }
    )
    wr.redshift.to_sql(
        update, con, table=t + "_k", schema="public", mode="upsert", primary_keys=["id"]
    )
    got = wr.redshift.read_sql_query(
        f"SELECT id, name FROM public.{t}_k ORDER BY id", con
    )
    assert got.values.tolist() == [[1, "a"], [2, "B"], [3, "c"], [4, "d"]]
    # add_new_columns reads Redshift's enable_case_sensitive_identifier
    wr.redshift.to_sql(
        df.assign(extra=["x", "y", "z"]),
        con,
        table=t,
        schema="public",
        add_new_columns=True,
    )
    assert "extra" in wr.redshift.read_sql_table(t, con, schema="public").columns
    big = pd.concat([df] * 100, ignore_index=True)
    wr.redshift.to_sql(
        big, con, table=t, schema="public", mode="overwrite", chunksize=50
    )
    chunks = wr.redshift.read_sql_query(f"SELECT * FROM public.{t}", con, chunksize=120)
    assert sum(len(c) for c in chunks) == 300
    cur = con.cursor()
    for suffix in ("", "_k"):
        cur.execute(f"DROP TABLE public.{t}{suffix}")
    con.commit()


def test_copy_and_unload_to_files(con, df):
    t, path = f"wr_cp_{U}", f"s3://{BUCKET}/{U}/"
    wr.redshift.copy(
        df, path=path + "stage/", con=con, table=t, schema="public", mode="overwrite"
    )
    assert wr.redshift.unload(
        f"SELECT * FROM public.{t}", path=path + "u/", con=con
    ).shape == (
        3,
        5,
    )
    wr.redshift.unload_to_files(
        f"SELECT id, name, flag FROM public.{t}",
        path=path + "parts/",
        con=con,
        partition_cols=["flag"],
        manifest=True,
    )
    keys = sorted(
        o["Key"].split(f"{U}/parts/")[1]
        for o in boto3.client("s3").list_objects_v2(
            Bucket=BUCKET, Prefix=f"{U}/parts/"
        )["Contents"]
    )
    assert keys == [
        "flag=false/0000_part_00.parquet",
        "flag=true/0000_part_00.parquet",
        "manifest",
    ]
    con.cursor().execute(f"DROP TABLE public.{t}")
    con.commit()
