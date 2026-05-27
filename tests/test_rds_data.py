"""Integration tests for the RDS Data API (boto3 'rds-data').

Requires the RDS Postgres engine:
    docker compose up -d rds

Unlike redshift-data, rds-data is synchronous (ExecuteStatement returns results
directly) and supports transactions.
"""

import json

import boto3
import pytest

from oblako.rds_data import start_in_thread
from oblako.rds_data.executor import RdsDataExecutor

CREDS = dict(region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
ARN = dict(
    resourceArn="arn:aws:rds:us-east-1:000000000000:cluster:analytics",
    secretArn="arn:aws:secretsmanager:us-east-1:000000000000:secret:db",
    database="oblako",
)
TX_ARN = {k: ARN[k] for k in ("resourceArn", "secretArn", "database")}


@pytest.fixture(scope="module")
def data():
    executor = RdsDataExecutor(host="localhost", port=5432, user="oblako",
                               password="oblako", database="oblako")
    url = start_in_thread(port=8016, executor=executor)
    return boto3.client("rds-data", endpoint_url=url, **CREDS)


@pytest.fixture
def table(data):
    data.execute_statement(sql="DROP TABLE IF EXISTS rdsd_test", **ARN)
    data.execute_statement(sql="CREATE TABLE rdsd_test (id INT PRIMARY KEY, name TEXT, amt FLOAT)", **ARN)
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
        **ARN, **extra,
    )


def test_execute_returns_records_synchronously(data, table):
    r = _insert(data, 1, "alice", 9.5)
    assert r["numberOfRecordsUpdated"] == 1
    r = data.execute_statement(
        sql="SELECT id, name, amt FROM rdsd_test ORDER BY id", includeResultMetadata=True, **ARN
    )
    assert [c["name"] for c in r["columnMetadata"]] == ["id", "name", "amt"]
    assert r["records"] == [[{"longValue": 1}, {"stringValue": "alice"}, {"doubleValue": 9.5}]]


def test_formatted_records_json(data, table):
    _insert(data, 1, "alice", 9.5)
    r = data.execute_statement(sql="SELECT id, name FROM rdsd_test", formatRecordsAs="JSON", **ARN)
    assert json.loads(r["formattedRecords"]) == [{"id": 1, "name": "alice"}]


def test_transaction_commit(data, table):
    tx = data.begin_transaction(**TX_ARN)["transactionId"]
    _insert(data, 1, "alice", 1.0, transactionId=tx)
    assert data.commit_transaction(
        resourceArn=ARN["resourceArn"], secretArn=ARN["secretArn"], transactionId=tx
    )["transactionStatus"] == "Transaction Committed"
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
        import pymysql
        conn = pymysql.connect(host="localhost", port=3306, user="oblako",
                               password="oblako", database="oblako", connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def mysql_data():
    if not _mysql_available():
        pytest.skip("MySQL engine not running on 3306")
    executor = RdsDataExecutor(host="localhost", port=3306, user="oblako",
                               password="oblako", database="oblako", engine="mysql")
    url = start_in_thread(port=8017, executor=executor)
    return boto3.client("rds-data", endpoint_url=url, **CREDS)


def test_mysql_execute_and_transaction(mysql_data):
    d = mysql_data
    d.execute_statement(sql="DROP TABLE IF EXISTS m_rdsd", **ARN)
    d.execute_statement(sql="CREATE TABLE m_rdsd (id INT PRIMARY KEY, name VARCHAR(50))", **ARN)
    d.execute_statement(
        sql="INSERT INTO m_rdsd VALUES (:id, :n)",
        parameters=[{"name": "id", "value": {"longValue": 1}}, {"name": "n", "value": {"stringValue": "alice"}}],
        **ARN,
    )
    r = d.execute_statement(sql="SELECT id, name FROM m_rdsd", includeResultMetadata=True, **ARN)
    assert r["records"] == [[{"longValue": 1}, {"stringValue": "alice"}]]
    assert [c["name"] for c in r["columnMetadata"]] == ["id", "name"]
    # transaction rollback leaves the table unchanged
    tx = d.begin_transaction(**TX_ARN)["transactionId"]
    d.execute_statement(sql="INSERT INTO m_rdsd VALUES (2, 'bob')", transactionId=tx, **ARN)
    d.rollback_transaction(resourceArn=ARN["resourceArn"], secretArn=ARN["secretArn"], transactionId=tx)
    r = d.execute_statement(sql="SELECT count(*) AS c FROM m_rdsd", **ARN)
    assert r["records"] == [[{"longValue": 1}]]
    d.execute_statement(sql="DROP TABLE m_rdsd", **ARN)


def test_batch_execute(data, table):
    r = data.batch_execute_statement(
        sql="INSERT INTO rdsd_test VALUES (:id, :n, :a)",
        parameterSets=[
            [{"name": "id", "value": {"longValue": 10}}, {"name": "n", "value": {"stringValue": "x"}}, {"name": "a", "value": {"doubleValue": 1.0}}],
            [{"name": "id", "value": {"longValue": 11}}, {"name": "n", "value": {"stringValue": "y"}}, {"name": "a", "value": {"doubleValue": 2.0}}],
        ],
        **ARN,
    )
    assert len(r["updateResults"]) == 2
    r = data.execute_statement(sql="SELECT count(*) FROM rdsd_test", **ARN)
    assert r["records"] == [[{"longValue": 2}]]
