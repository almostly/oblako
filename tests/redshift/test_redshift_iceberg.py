"""Integration tests: Redshift's Apache Iceberg tables on redshift-local.

Needs the Redshift engine on 5439, S3Proxy on 9000 and oblako's Iceberg REST
catalog on 8181. ``CREATE EXTERNAL SCHEMA ... FROM DATA CATALOG`` maps a schema
to a Glue database; ``CREATE TABLE ... USING ICEBERG`` writes the table to the
catalog, where PyIceberg, Trino and the Glue API see it, and tables other engines
create show up in Redshift.
"""

import os
import uuid
from typing import TYPE_CHECKING, cast

import boto3
import psycopg
import pytest

from oblako import ports

if TYPE_CHECKING:
    from typing_extensions import LiteralString

BUCKET = "oblako-iceberg-tests"
DB = f"rs_iceberg_{uuid.uuid4().hex[:6]}"
SCHEMA = f"lake_{uuid.uuid4().hex[:6]}"


def _catalog():
    os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")
    catalog = pytest.importorskip("pyiceberg.catalog")
    return catalog.load_catalog(
        "oblako",
        type="rest",
        uri=f"http://localhost:{ports.ICEBERG}",
        **{
            "py-io-impl": "pyiceberg.io.fsspec.FsspecFileIO",
            "s3.endpoint": f"http://localhost:{ports.S3}",
            "s3.access-key-id": "oblako",
            "s3.secret-access-key": "oblako",
            "s3.path-style-access": "true",
            "s3.region": "us-east-1",
        },
    )


@pytest.fixture(scope="module")
def cat():
    try:
        c = _catalog()
        c.list_namespaces()
    except Exception as e:  # no catalog running
        pytest.skip(f"oblako's Iceberg catalog isn't running: {e}")
    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://localhost:{ports.S3}",
        aws_access_key_id="oblako",
        aws_secret_access_key="oblako",
        region_name="us-east-1",
    )
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    return c


@pytest.fixture(scope="module")
def rs(cat):
    try:
        conn = psycopg.connect(
            host="localhost",
            port=5439,
            user="oblako",
            password="oblako",
            dbname="oblako",
            sslmode="require",
            autocommit=True,
        )
    except psycopg.OperationalError as e:
        pytest.skip(f"redshift-local isn't running: {e}")
    _exec(
        conn,
        f"CREATE EXTERNAL SCHEMA {SCHEMA} FROM DATA CATALOG DATABASE '{DB}' "
        "IAM_ROLE default CREATE EXTERNAL DATABASE IF NOT EXISTS",
    )
    yield conn
    _exec(conn, f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def _location(name: str) -> str:
    return f"s3://{BUCKET}/{DB}/{name}-{uuid.uuid4().hex[:6]}/"


def _exec(conn, query: str):
    # the tests build their SQL from generated names, never from input
    return conn.execute(cast("LiteralString", query))


def _rows(conn, query: str):
    return _exec(conn, query).fetchall()


def test_external_schema_is_listed(rs):
    assert _rows(
        rs,
        f"SELECT databasename FROM svv_external_schemas WHERE schemaname = '{SCHEMA}'",
    ) == [(DB,)]


def test_create_insert_update_delete(rs, cat):
    _exec(
        rs,
        f"CREATE TABLE {SCHEMA}.orders (order_id int, order_date date, "
        "total decimal(10,2), status varchar) USING ICEBERG "
        f"LOCATION '{_location('orders')}' PARTITIONED BY (month(order_date)) "
        "TABLE PROPERTIES ('compression_type'='snappy')",
    )
    _exec(
        rs,
        f"INSERT INTO {SCHEMA}.orders VALUES (1, '2024-10-30', 299.99, 'new'), "
        "(2, '2024-11-02', 150.75, 'new')",
    )
    _exec(rs, f"UPDATE {SCHEMA}.orders SET status = 'shipped' WHERE order_id = 1")
    _exec(rs, f"DELETE FROM {SCHEMA}.orders WHERE order_id = 2")
    assert _rows(rs, f"SELECT order_id, status FROM {SCHEMA}.orders") == [
        (1, "shipped")
    ]
    # the same table, read by another engine
    table = cat.load_table((DB, "orders")).scan().to_arrow().to_pylist()
    assert [(r["order_id"], r["status"]) for r in table] == [(1, "shipped")]
    assert str(cat.load_table((DB, "orders")).spec().fields[0].transform) == "month"


def test_rollback_writes_nothing(rs):
    _exec(
        rs,
        f"CREATE TABLE {SCHEMA}.events (id int) USING ICEBERG "
        f"LOCATION '{_location('events')}'",
    )
    with rs.transaction(force_rollback=True):
        _exec(rs, f"INSERT INTO {SCHEMA}.events VALUES (1)")
    assert _rows(rs, f"SELECT count(*) FROM {SCHEMA}.events") == [(0,)]


def test_one_write_per_transaction(rs):
    _exec(
        rs,
        f"CREATE TABLE {SCHEMA}.once (id int) USING ICEBERG "
        f"LOCATION '{_location('once')}'",
    )
    with pytest.raises(psycopg.Error, match="one write"):
        with rs.transaction():
            _exec(rs, f"INSERT INTO {SCHEMA}.once VALUES (1)")
            _exec(rs, f"INSERT INTO {SCHEMA}.once VALUES (2)")
    assert _rows(rs, f"SELECT count(*) FROM {SCHEMA}.once") == [(0,)]


def test_ctas_and_join_with_a_local_table(rs):
    _exec(rs, "DROP TABLE IF EXISTS public.iceberg_customers")
    _exec(rs, "CREATE TABLE public.iceberg_customers (id int, name varchar(20))")
    _exec(rs, "INSERT INTO public.iceberg_customers VALUES (1, 'Ann'), (2, 'Bo')")
    _exec(
        rs,
        f"CREATE TABLE {SCHEMA}.customers_copy USING ICEBERG "
        f"LOCATION '{_location('copy')}' AS SELECT * FROM public.iceberg_customers",
    )
    assert _rows(
        rs,
        f"SELECT c.name FROM {SCHEMA}.customers_copy c "
        "JOIN public.iceberg_customers l USING (id) ORDER BY 1",
    ) == [("Ann",), ("Bo",)]
    _exec(rs, "DROP TABLE public.iceberg_customers")


def test_show_table(rs):
    location = _location("shown")
    _exec(
        rs,
        f"CREATE TABLE {SCHEMA}.shown (id int, price decimal(5, 2)) USING ICEBERG "
        f"LOCATION '{location}' PARTITIONED BY (bucket(16, id))",
    )
    (ddl,) = _rows(rs, f"SHOW TABLE {SCHEMA}.shown")[0]
    # Redshift Serverless's own output, for the same statement (2026-10)
    assert ddl == (
        f"CREATE TABLE {SCHEMA}.shown (id int,\n"
        "price decimal(5, 2))\n"
        "USING ICEBERG\n"
        f"LOCATION '{location.rstrip('/')}'\n"
        "PARTITIONED BY (BUCKET(16, id))\n"
        "TABLE PROPERTIES ('format-version'='2', 'compression_type'='zstd');"
    )


def test_a_table_from_another_engine_shows_up(rs, cat):
    pa = pytest.importorskip("pyarrow")
    ns = f"{DB}_spark"
    cat.create_namespace(ns)
    table = cat.create_table(
        (ns, "clicks"), schema=pa.schema([("id", pa.int64()), ("page", pa.string())])
    )
    table.append(pa.table({"id": pa.array([1, 2], pa.int64()), "page": ["/", "/docs"]}))
    schema = f"{SCHEMA}_spark"
    _exec(rs, f"CREATE EXTERNAL SCHEMA {schema} FROM DATA CATALOG DATABASE '{ns}'")
    try:
        assert _rows(rs, f"SELECT page FROM {schema}.clicks ORDER BY id") == [
            ("/",),
            ("/docs",),
        ]
        _exec(rs, f"INSERT INTO {schema}.clicks VALUES (3, '/blog')")
        assert cat.load_table((ns, "clicks")).scan().to_arrow().num_rows == 3
        # a table created later appears when the schema is declared again
        cat.create_table((ns, "later"), schema=pa.schema([("id", pa.int64())]))
        _exec(
            rs,
            f"CREATE EXTERNAL SCHEMA IF NOT EXISTS {schema} "
            f"FROM DATA CATALOG DATABASE '{ns}'",
        )
        assert _rows(rs, f"SELECT count(*) FROM {schema}.later") == [(0,)]
    finally:
        _exec(rs, f"DROP SCHEMA {schema} CASCADE")


def test_glue_sees_the_table(rs):
    from oblako.services.glue_catalog import GlueCatalogService

    _exec(
        rs,
        f"CREATE TABLE {SCHEMA}.in_glue (id int) USING ICEBERG "
        f"LOCATION '{_location('in-glue')}'",
    )
    glue = GlueCatalogService().get_client()
    table = glue.get_table(DatabaseName=DB, Name="in_glue")["Table"]
    assert table["Parameters"]["table_type"].upper() == "ICEBERG"


def test_drop_table_keeps_the_files(rs, cat):
    _exec(
        rs,
        f"CREATE TABLE {SCHEMA}.dropped (id int) USING ICEBERG "
        f"LOCATION '{_location('dropped')}'",
    )
    _exec(rs, f"INSERT INTO {SCHEMA}.dropped VALUES (1)")
    location = cat.load_table((DB, "dropped")).location()
    _exec(rs, f"DROP TABLE {SCHEMA}.dropped")
    assert not cat.table_exists((DB, "dropped"))
    assert _rows(
        rs, "SELECT count(*) FROM svv_external_tables WHERE tablename = 'dropped'"
    ) == [(0,)]
    bucket, _, prefix = location.removeprefix("s3://").partition("/")
    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://localhost:{ports.S3}",
        aws_access_key_id="oblako",
        aws_secret_access_key="oblako",
        region_name="us-east-1",
    )
    assert s3.list_objects_v2(Bucket=bucket, Prefix=prefix)["KeyCount"] > 0


@pytest.mark.parametrize(
    "columns, clauses, error",
    [
        # the messages Redshift Serverless gave for the same statements (2026-10)
        ("(id int)", "", 'Empty location for Iceberg table "bad"'),
        ("(id int PRIMARY KEY)", "LOCATION '{loc}'", "Columns constraints"),
        ("(id int, s varchar DEFAULT 'x')", "LOCATION '{loc}'", "Columns constraints"),
        ("(id int, s varchar(20))", "LOCATION '{loc}'", "VARCHAR\\(N\\) specifiying"),
        (
            "(id int)",
            "LOCATION '{loc}' TABLE PROPERTIES ('owner'='me')",
            "cannot be used in the PROPERTIES clause",
        ),
        (
            "(id int)",
            "LOCATION '{loc}' TABLE PROPERTIES ('format-version'='3')",
            '"3" is not a valid value for the "format-version" property',
        ),
        (
            "(id int, d date)",
            "LOCATION '{loc}' PARTITIONED BY (bucket(4, d), year(d))",
            "used in multiple transform functions",
        ),
    ],
)
def test_refused_as_redshift_refuses(rs, columns, clauses, error):
    clauses = clauses.format(loc=_location("bad"))
    with pytest.raises(psycopg.Error, match=error):
        _exec(rs, f"CREATE TABLE {SCHEMA}.bad {columns} USING ICEBERG {clauses}")


def test_not_null_is_kept_and_enforced(rs, cat):
    _exec(
        rs,
        f"CREATE TABLE {SCHEMA}.required (id int NOT NULL, note varchar) "
        f"USING ICEBERG LOCATION '{_location('required')}'",
    )
    assert cat.load_table((DB, "required")).schema().find_field("id").required
    with pytest.raises(
        psycopg.Error, match="Cannot insert a NULL value into column id"
    ):
        _exec(rs, f"INSERT INTO {SCHEMA}.required VALUES (NULL, 'x')")
    (ddl,) = _rows(rs, f"SHOW TABLE {SCHEMA}.required")[0]
    assert ddl.startswith(
        f"CREATE TABLE {SCHEMA}.required (id int NOT NULL,\nnote varchar)"
    )


def test_location_must_be_empty(rs):
    location = _location("taken")
    _exec(
        rs, f"CREATE TABLE {SCHEMA}.taken (id int) USING ICEBERG LOCATION '{location}'"
    )
    _exec(rs, f"INSERT INTO {SCHEMA}.taken VALUES (1)")
    with pytest.raises(psycopg.Error, match="contains existing objects"):
        _exec(
            rs,
            f"CREATE TABLE {SCHEMA}.taken_again (id int) USING ICEBERG "
            f"LOCATION '{location}'",
        )


def test_redshift_connector_parameters(rs):
    redshift_connector = pytest.importorskip("redshift_connector")
    _exec(
        rs,
        f"CREATE TABLE {SCHEMA}.params (id int, note varchar) USING ICEBERG "
        f"LOCATION '{_location('params')}'",
    )
    conn = redshift_connector.connect(
        host="localhost",
        port=5439,
        database="oblako",
        user="oblako",
        password="oblako",
        ssl=True,
        sslmode="verify-ca",
    )
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.executemany(
            f"INSERT INTO {SCHEMA}.params VALUES (%s, %s)", [(1, "a"), (2, "b")]
        )
        cur.execute(f"SELECT note FROM {SCHEMA}.params WHERE id = %s", (2,))
        assert [list(r) for r in cur.fetchall()] == [["b"]]
    finally:
        conn.close()


def test_ddl_returns_no_rows_as_on_redshift(rs):
    cur = _exec(
        rs,
        f"CREATE TABLE {SCHEMA}.quiet (id int) USING ICEBERG "
        f"LOCATION '{_location('quiet')}'",
    )
    assert cur.description is None


def test_merge_into_an_iceberg_table(rs, cat):
    _exec(
        rs,
        f"CREATE TABLE {SCHEMA}.stock (sku varchar, qty int) USING ICEBERG "
        f"LOCATION '{_location('stock')}'",
    )
    _exec(rs, f"INSERT INTO {SCHEMA}.stock VALUES ('a', 1), ('b', 2), ('c', 3)")
    _exec(rs, "DROP TABLE IF EXISTS public.stock_delta")
    _exec(rs, "CREATE TABLE public.stock_delta (sku varchar, qty int)")
    _exec(rs, "INSERT INTO public.stock_delta VALUES ('a', 10), ('c', 0), ('d', 4)")
    # Redshift's form: the target named by its table name in ON and SET
    cur = _exec(
        rs,
        f"MERGE INTO {SCHEMA}.stock USING public.stock_delta s ON stock.sku = s.sku "
        "WHEN MATCHED AND s.qty = 0 THEN DELETE "
        "WHEN MATCHED THEN UPDATE SET qty = stock.qty + s.qty "
        "WHEN NOT MATCHED THEN INSERT VALUES (s.sku, s.qty)",
    )
    assert cur.description is None
    assert _rows(rs, f"SELECT sku, qty FROM {SCHEMA}.stock ORDER BY sku") == [
        ("a", 11),
        ("b", 2),
        ("d", 4),
    ]
    snapshot = cat.load_table((DB, "stock")).scan().to_arrow().to_pylist()
    assert sorted((r["sku"], r["qty"]) for r in snapshot) == [
        ("a", 11),
        ("b", 2),
        ("d", 4),
    ]
    _exec(rs, "DROP TABLE public.stock_delta")


def test_merge_into_a_local_table_is_untouched(rs):
    _exec(rs, "DROP TABLE IF EXISTS public.merge_local")
    _exec(rs, "CREATE TABLE public.merge_local (id int, v int)")
    _exec(rs, "INSERT INTO public.merge_local VALUES (1, 1)")
    cur = _exec(
        rs,
        "MERGE INTO public.merge_local USING (SELECT 1 AS id, 5 AS v) s "
        "ON merge_local.id = s.id WHEN MATCHED THEN UPDATE SET v = s.v",
    )
    assert cur.statusmessage == "MERGE 1"
    _exec(rs, "DROP TABLE public.merge_local")


def test_alter_table_changes_the_iceberg_table(rs, cat):
    t = f"{SCHEMA}.evolving"
    _exec(
        rs,
        f"CREATE TABLE {t} (id int, amount real, d date) USING ICEBERG "
        f"LOCATION '{_location('evolving')}' PARTITIONED BY (year(d))",
    )
    _exec(rs, f"INSERT INTO {t} VALUES (1, 1.5, '2026-01-02')")
    _exec(rs, f"ALTER TABLE {t} RENAME COLUMN amount TO price")
    _exec(rs, f"ALTER TABLE {t} ADD COLUMN note varchar")
    _exec(rs, f"ALTER TABLE {t} ALTER COLUMN id TYPE bigint")
    _exec(rs, f"ALTER TABLE {t} ALTER COLUMN price TYPE double precision")
    _exec(rs, f"ALTER TABLE {t} SET TABLE PROPERTIES ('compression_type'='snappy')")
    _exec(rs, f"ALTER TABLE {t} REPLACE PARTITION FIELD year(d) WITH month(d)")
    _exec(rs, f"ALTER TABLE {t} ADD PARTITION FIELD bucket(4, id)")
    _exec(rs, f"INSERT INTO {t} VALUES (2, 2.5, '2026-02-03', 'new')")
    assert _rows(rs, f"SELECT id, price, note FROM {t} ORDER BY id") == [
        (1, 1.5, None),
        (2, 2.5, "new"),
    ]
    ice = cat.load_table((DB, "evolving"))
    fields = {f.name: str(f.field_type) for f in ice.schema().fields}
    assert fields == {"id": "long", "price": "double", "d": "date", "note": "string"}
    assert [str(f.transform) for f in ice.spec().fields] == ["month", "bucket[4]"]
    assert ice.properties["write.parquet.compression-codec"] == "snappy"
    _exec(rs, f"ALTER TABLE {t} DROP PARTITION FIELD bucket(4, id)")
    _exec(rs, f"ALTER TABLE {t} DROP COLUMN note")
    (ddl,) = _rows(rs, f"SHOW TABLE {t}")[0]
    assert ddl.startswith(
        f"CREATE TABLE {t} (id bigint,\nprice double precision,\nd date)"
    )
    assert "PARTITIONED BY (MONTH(d))" in ddl


@pytest.mark.parametrize(
    "action, error",
    [
        ("ALTER COLUMN id TYPE smallint", "widens only"),
        ("DROP COLUMN d", "partition spec"),
        ("ADD COLUMN s varchar DEFAULT 'x'", "Default values"),
        ("ADD COLUMN s varchar(20)", "VARCHAR\\(N\\)"),
    ],
)
def test_alter_table_refused(rs, action, error):
    t = f"{SCHEMA}.fixed_{uuid.uuid4().hex[:6]}"
    _exec(
        rs,
        f"CREATE TABLE {t} (id int, d date) USING ICEBERG "
        f"LOCATION '{_location('fixed')}' PARTITIONED BY (day(d))",
    )
    with pytest.raises(psycopg.Error, match=error):
        _exec(rs, f"ALTER TABLE {t} {action}")


def test_awsdatacatalog_three_part_names(rs, cat):
    """Redshift's auto-mounted Data Catalog: no CREATE EXTERNAL SCHEMA first."""
    pa = pytest.importorskip("pyarrow")
    db = f"{DB}_auto"
    cat.create_namespace(db)
    seen = cat.create_table((db, "seen"), schema=pa.schema([("id", pa.int64())]))
    seen.append(pa.table({"id": pa.array([7], pa.int64())}))
    assert _rows(rs, f"SELECT id FROM awsdatacatalog.{db}.seen") == [(7,)]
    _exec(
        rs,
        f"CREATE TABLE awsdatacatalog.{db}.made (id int, v varchar) USING ICEBERG "
        f"LOCATION '{_location('made')}'",
    )
    _exec(rs, f"INSERT INTO awsdatacatalog.{db}.made VALUES (1, 'a')")
    _exec(
        rs,
        f"MERGE INTO awsdatacatalog.{db}.made USING (SELECT 1 AS id, 'b' AS v) s "
        "ON made.id = s.id WHEN MATCHED THEN UPDATE SET v = s.v",
    )
    assert _rows(rs, f"SELECT id, v FROM awsdatacatalog.{db}.made") == [(1, "b")]
    assert cat.load_table((db, "made")).scan().to_arrow().num_rows == 1
    _exec(rs, f'DROP SCHEMA "awsdatacatalog.{db}" CASCADE')
