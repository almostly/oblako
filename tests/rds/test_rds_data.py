"""Integration tests for the RDS Data API (boto3 'rds-data').

Requires the RDS Postgres engine:
    docker compose up -d rds

Unlike redshift-data, rds-data is synchronous (ExecuteStatement returns results
directly) and supports transactions.
"""

import importlib
import json

import boto3
import pytest

from oblako import ports
from oblako.engines.rds_data import start_in_thread
from oblako.engines.rds_data.executor import RdsDataExecutor
from tests.ports import free_port

CREDS = dict(
    region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
)
ARN = dict(
    resourceArn="arn:aws:rds:us-east-1:000000000000:cluster:analytics",
    secretArn="arn:aws:secretsmanager:us-east-1:000000000000:secret:db",
    database="oblako",
)
TX_ARN = {k: ARN[k] for k in ("resourceArn", "secretArn", "database")}


@pytest.fixture(scope="module")
def data():
    executor = RdsDataExecutor(
        host="localhost",
        port=ports.RDS_PG,  # OBLAKO_PORT_RDS_PG moves it when 5432 is taken
        user="oblako",
        password="oblako",
        database="oblako",
    )
    url = start_in_thread(port=free_port(), executor=executor)
    return boto3.client("rds-data", endpoint_url=url, **CREDS)


@pytest.fixture
def table(data):
    data.execute_statement(sql="DROP TABLE IF EXISTS rdsd_test", **ARN)
    data.execute_statement(
        sql="CREATE TABLE rdsd_test (id INT PRIMARY KEY, name TEXT, amt FLOAT)", **ARN
    )
    yield
    data.execute_statement(sql="DROP TABLE IF EXISTS rdsd_test", **ARN)


def _insert(data, id_, name, amt, **extra):
    return data.execute_statement(
        sql="INSERT INTO rdsd_test VALUES (:id, :name, :amt)",
        parameters=[
            {"name": "id", "value": {"longValue": id_}},
            {"name": "name", "value": {"stringValue": name}},
            {"name": "amt", "value": {"doubleValue": amt}},
        ],
        **ARN,
        **extra,
    )


def test_execute_returns_records_synchronously(data, table):
    r = _insert(data, 1, "alice", 9.5)
    assert r["numberOfRecordsUpdated"] == 1
    r = data.execute_statement(
        sql="SELECT id, name, amt FROM rdsd_test ORDER BY id",
        includeResultMetadata=True,
        **ARN,
    )
    assert [c["name"] for c in r["columnMetadata"]] == ["id", "name", "amt"]
    assert r["records"] == [
        [{"longValue": 1}, {"stringValue": "alice"}, {"doubleValue": 9.5}]
    ]


def test_formatted_records_json(data, table):
    _insert(data, 1, "alice", 9.5)
    r = data.execute_statement(
        sql="SELECT id, name FROM rdsd_test", formatRecordsAs="JSON", **ARN
    )
    assert json.loads(r["formattedRecords"]) == [{"id": 1, "name": "alice"}]


def test_transaction_commit(data, table):
    tx = data.begin_transaction(**TX_ARN)["transactionId"]
    _insert(data, 1, "alice", 1.0, transactionId=tx)
    assert (
        data.commit_transaction(
            resourceArn=ARN["resourceArn"], secretArn=ARN["secretArn"], transactionId=tx
        )["transactionStatus"]
        == "Transaction Committed"
    )
    r = data.execute_statement(sql="SELECT count(*) FROM rdsd_test", **ARN)
    assert r["records"] == [[{"longValue": 1}]]


def test_transaction_rollback(data, table):
    tx = data.begin_transaction(**TX_ARN)["transactionId"]
    _insert(data, 1, "alice", 1.0, transactionId=tx)
    data.rollback_transaction(
        resourceArn=ARN["resourceArn"], secretArn=ARN["secretArn"], transactionId=tx
    )
    r = data.execute_statement(sql="SELECT count(*) FROM rdsd_test", **ARN)
    assert r["records"] == [[{"longValue": 0}]]


def _mysql_available() -> bool:
    try:
        # an optional dependency (the mysql extra), as in the engine's executor
        pymysql = importlib.import_module("pymysql")
        conn = pymysql.connect(
            host="localhost",
            port=ports.RDS_MYSQL,
            user="oblako",
            password="oblako",
            database="oblako",
            connect_timeout=2,
        )
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def mysql_data():
    if not _mysql_available():
        pytest.skip("MySQL engine not running on 3306")
    executor = RdsDataExecutor(
        host="localhost",
        port=3306,
        user="oblako",
        password="oblako",
        database="oblako",
        engine="mysql",
    )
    url = start_in_thread(port=free_port(), executor=executor)
    return boto3.client("rds-data", endpoint_url=url, **CREDS)


def test_mysql_execute_and_transaction(mysql_data):
    d = mysql_data
    d.execute_statement(sql="DROP TABLE IF EXISTS m_rdsd", **ARN)
    d.execute_statement(
        sql="CREATE TABLE m_rdsd (id INT PRIMARY KEY, name VARCHAR(50))", **ARN
    )
    d.execute_statement(
        sql="INSERT INTO m_rdsd VALUES (:id, :n)",
        parameters=[
            {"name": "id", "value": {"longValue": 1}},
            {"name": "n", "value": {"stringValue": "alice"}},
        ],
        **ARN,
    )
    r = d.execute_statement(
        sql="SELECT id, name FROM m_rdsd", includeResultMetadata=True, **ARN
    )
    assert r["records"] == [[{"longValue": 1}, {"stringValue": "alice"}]]
    assert [c["name"] for c in r["columnMetadata"]] == ["id", "name"]
    # transaction rollback leaves the table unchanged
    tx = d.begin_transaction(**TX_ARN)["transactionId"]
    d.execute_statement(
        sql="INSERT INTO m_rdsd VALUES (2, 'bob')", transactionId=tx, **ARN
    )
    d.rollback_transaction(
        resourceArn=ARN["resourceArn"], secretArn=ARN["secretArn"], transactionId=tx
    )
    r = d.execute_statement(sql="SELECT count(*) AS c FROM m_rdsd", **ARN)
    assert r["records"] == [[{"longValue": 1}]]
    d.execute_statement(sql="DROP TABLE m_rdsd", **ARN)


def test_batch_execute(data, table):
    r = data.batch_execute_statement(
        sql="INSERT INTO rdsd_test VALUES (:id, :n, :a)",
        parameterSets=[
            [
                {"name": "id", "value": {"longValue": 10}},
                {"name": "n", "value": {"stringValue": "x"}},
                {"name": "a", "value": {"doubleValue": 1.0}},
            ],
            [
                {"name": "id", "value": {"longValue": 11}},
                {"name": "n", "value": {"stringValue": "y"}},
                {"name": "a", "value": {"doubleValue": 2.0}},
            ],
        ],
        **ARN,
    )
    assert len(r["updateResults"]) == 2
    r = data.execute_statement(sql="SELECT count(*) FROM rdsd_test", **ARN)
    assert r["records"] == [[{"longValue": 2}]]


def test_decimal_is_a_string_by_default(data):
    sql = "SELECT 13.20::numeric(10, 2) AS price, 7 AS n"
    r = data.execute_statement(sql=sql, formatRecordsAs="JSON", **ARN)
    assert json.loads(r["formattedRecords"]) == [{"price": "13.20", "n": 7}]
    r = data.execute_statement(sql=sql, **ARN)
    assert r["records"][0] == [{"stringValue": "13.20"}, {"longValue": 7}]


def test_result_set_options(data):
    sql = "SELECT 13.20::numeric(10, 2) AS price, 4.00::numeric AS whole, 7 AS n"
    r = data.execute_statement(
        sql=sql,
        formatRecordsAs="JSON",
        resultSetOptions={
            "decimalReturnType": "DOUBLE_OR_LONG",
            "longReturnType": "STRING",
        },
        **ARN,
    )
    assert json.loads(r["formattedRecords"]) == [{"price": 13.2, "whole": 4, "n": "7"}]


def test_arrays_come_back_as_array_values(data):
    # the RDS Data API returns an array as arrayValue (the Redshift Data API as text)
    out = data.execute_statement(
        sql="SELECT ARRAY['a', 'b c']::text[], ARRAY[1, 2], ARRAY[[1, 2], [3, 4]], "
        "'{}'::text[]",
        **ARN,
    )
    assert out["records"] == [
        [
            {"arrayValue": {"stringValues": ["a", "b c"]}},
            {"arrayValue": {"longValues": [1, 2]}},
            {
                "arrayValue": {
                    "arrayValues": [{"longValues": [1, 2]}, {"longValues": [3, 4]}]
                }
            },
            {"arrayValue": {"stringValues": []}},
        ]
    ]
    out = data.execute_statement(
        sql="SELECT ARRAY['a', 'b'] AS tags", formatRecordsAs="JSON", **ARN
    )
    assert json.loads(out["formattedRecords"]) == [{"tags": ["a", "b"]}]
