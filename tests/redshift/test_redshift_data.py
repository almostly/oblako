"""Integration tests for the Redshift management + Data APIs.

Requires running services:
    docker compose up -d redshift moto   # the Redshift engine on 5439, moto on 5500

Exercises real boto3 'redshift' (control plane) and 'redshift-data' (executing
SQL against the Redshift engine) clients.
"""

import os
import socket

import boto3
import pytest
from botocore.exceptions import ClientError

from oblako.engines.redshift_data import start_in_thread
from oblako.engines.redshift_data.executor import RedshiftDataExecutor

CREDS = dict(
    region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
)


RS_PORT = int(os.environ.get("OBLAKO_TEST_RS_PORT", "5439"))


def _free_port() -> int:
    """Return a port nothing listens on, so the test runs this server, not another."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def data_client():
    executor = RedshiftDataExecutor(
        host="localhost",
        port=RS_PORT,
        user="oblako",
        password="oblako",
        database="oblako",
    )
    url = start_in_thread(port=_free_port(), executor=executor)
    return boto3.client("redshift-data", endpoint_url=url, **CREDS)


@pytest.fixture(scope="module")
def control_client():
    return boto3.client("redshift", endpoint_url="http://localhost:5500", **CREDS)


@pytest.fixture
def clean_table(data_client):
    data_client.execute_statement(
        Database="oblako", Sql="DROP TABLE IF EXISTS rsd_test"
    )
    sid = data_client.execute_statement(
        Database="oblako",
        Sql="CREATE TABLE rsd_test (id INT, name TEXT, amount FLOAT)",
    )["Id"]
    assert data_client.describe_statement(Id=sid)["Status"] == "FINISHED"
    yield
    data_client.execute_statement(
        Database="oblako", Sql="DROP TABLE IF EXISTS rsd_test"
    )


def test_execute_and_get_result(data_client, clean_table):
    data_client.execute_statement(
        Database="oblako",
        Sql="INSERT INTO rsd_test VALUES (1, 'alice', 9.5), (2, 'bob', 3.0)",
    )
    sid = data_client.execute_statement(
        Database="oblako", Sql="SELECT id, name, amount FROM rsd_test ORDER BY id"
    )["Id"]
    desc = data_client.describe_statement(Id=sid)
    assert desc["Status"] == "FINISHED"
    assert desc["ResultRows"] == 2

    res = data_client.get_statement_result(Id=sid)
    assert res["TotalNumRows"] == 2
    assert [c["name"] for c in res["ColumnMetadata"]] == ["id", "name", "amount"]
    first = res["Records"][0]
    assert first[0] == {"longValue": 1}
    assert first[1] == {"stringValue": "alice"}
    assert first[2] == {"doubleValue": 9.5}


def test_named_parameters(data_client, clean_table):
    data_client.execute_statement(
        Database="oblako",
        Sql="INSERT INTO rsd_test VALUES (1, 'x', 1), (2, 'y', 2), (3, 'z', 3)",
    )
    sid = data_client.execute_statement(
        Database="oblako",
        Sql="SELECT count(*) AS c FROM rsd_test WHERE id >= :min_id",
        Parameters=[{"name": "min_id", "value": "2"}],
    )["Id"]
    res = data_client.get_statement_result(Id=sid)
    assert res["Records"][0][0] == {"longValue": 2}


def test_failed_statement(data_client):
    sid = data_client.execute_statement(
        Database="oblako", Sql="SELECT * FROM no_such_table"
    )["Id"]
    desc = data_client.describe_statement(Id=sid)
    assert desc["Status"] == "FAILED"
    assert "no_such_table" in desc.get("Error", "")


def test_batch_takes_at_most_40_statements(data_client):
    """As on AWS: 40 statements run, 41 are refused before any of them runs."""
    sid = data_client.batch_execute_statement(
        Database="oblako", Sqls=["SELECT 1"] * 40
    )["Id"]
    assert data_client.describe_statement(Id=sid)["Status"] == "FINISHED"
    with pytest.raises(ClientError, match="less than or equal to 40") as err:
        data_client.batch_execute_statement(Database="oblako", Sqls=["SELECT 1"] * 41)
    assert err.value.response["Error"]["Code"] == "ValidationException"


def _status(client, sid: str) -> str:
    return client.describe_statement(Id=sid)["Status"]


def _count(client, sql: str) -> int:
    sid = client.execute_statement(Database="oblako", Sql=sql)["Id"]
    return client.get_statement_result(Id=sid)["Records"][0][0]["longValue"]


@pytest.fixture
def no_rsd_tables(data_client):
    """Drop the rsd_s* tables the session tests make, before and after."""

    def drop():
        for i in range(45):
            data_client.execute_statement(
                Database="oblako", Sql=f"DROP TABLE IF EXISTS rsd_s{i}"
            )

    drop()
    yield
    drop()


def test_a_session_shares_one_transaction(data_client, no_rsd_tables):
    """As on AWS: BEGIN opens a session; a failure and ROLLBACK leave nothing."""
    first = data_client.execute_statement(
        Database="oblako", Sql="BEGIN", SessionKeepAliveSeconds=300
    )
    sid = first["SessionId"]
    for i in range(45):
        sql = "SELECT 1/0" if i == 30 else f"CREATE TABLE rsd_s{i} (id int)"
        out = data_client.execute_statement(Sql=sql, SessionId=sid)
        assert out["SessionId"] == sid
        if i == 30:
            assert _status(data_client, out["Id"]) == "FAILED"
            break
    data_client.execute_statement(Sql="ROLLBACK", SessionId=sid)
    assert (
        _count(data_client, "SELECT count(*) FROM pg_tables WHERE tablename ~ '^rsd_s'")
        == 0
    )


def test_a_session_commits_all_its_statements(data_client, no_rsd_tables):
    sid = data_client.execute_statement(
        Database="oblako", Sql="BEGIN", SessionKeepAliveSeconds=300
    )["SessionId"]
    for i in range(45):
        data_client.execute_statement(
            Sql=f"CREATE TABLE rsd_s{i} (id int)", SessionId=sid
        )
    data_client.execute_statement(Sql="COMMIT", SessionId=sid)
    assert (
        _count(data_client, "SELECT count(*) FROM pg_tables WHERE tablename ~ '^rsd_s'")
        == 45
    )


def test_a_session_refuses_a_target_and_an_unknown_id(data_client):
    sid = data_client.execute_statement(
        Database="oblako", Sql="SELECT 1", SessionKeepAliveSeconds=60
    )["SessionId"]
    with pytest.raises(ClientError, match="SessionId can't be used with Database"):
        data_client.execute_statement(Sql="SELECT 1", SessionId=sid, Database="oblako")
    with pytest.raises(ClientError, match="doesn't exist or has expired"):
        data_client.execute_statement(
            Sql="SELECT 1", SessionId="00000000-0000-0000-0000-000000000000"
        )


def test_a_batch_is_one_transaction(data_client, no_rsd_tables):
    """A failing statement rolls the batch back; the ones after it are ABORTED."""
    out = data_client.batch_execute_statement(
        Database="oblako",
        Sqls=[
            "CREATE TABLE rsd_s0 (id int)",
            "SELECT 1/0",
            "CREATE TABLE rsd_s1 (id int)",
        ],
    )
    desc = data_client.describe_statement(Id=out["Id"])
    assert desc["Status"] == "FAILED"
    assert [s["Status"] for s in desc["SubStatements"]] == [
        "FINISHED",
        "FAILED",
        "ABORTED",
    ]
    assert (
        _count(data_client, "SELECT count(*) FROM pg_tables WHERE tablename ~ '^rsd_s'")
        == 0
    )


def test_get_statement_result_not_found(data_client):
    with pytest.raises(data_client.exceptions.ResourceNotFoundException):
        data_client.get_statement_result(Id="00000000-0000-0000-0000-000000000000")


def test_catalog_operations(data_client, clean_table):
    tables = data_client.list_tables(Database="oblako")["Tables"]
    assert any(t["name"] == "rsd_test" for t in tables)
    cols = data_client.describe_table(Database="oblako", Table="rsd_test")["ColumnList"]
    assert [c["name"] for c in cols] == ["id", "name", "amount"]
    assert "oblako" in data_client.list_databases(Database="oblako")["Databases"]


def test_redshift_udf_via_data_api(data_client):
    sid = data_client.execute_statement(
        Database="oblako", Sql="SELECT json_array_length('[1,2,3,4,5]') AS n"
    )["Id"]
    res = data_client.get_statement_result(Id=sid)
    assert res["Records"][0][0] == {"longValue": 5}


def test_control_plane_cluster_lifecycle(control_client):
    cid = "pytest-dw"
    try:
        control_client.delete_cluster(
            ClusterIdentifier=cid, SkipFinalClusterSnapshot=True
        )
    except Exception:
        pass
    control_client.create_cluster(
        ClusterIdentifier=cid,
        NodeType="ra3.xlplus",
        NumberOfNodes=3,
        MasterUsername="oblako",
        MasterUserPassword="Oblako123",
        DBName="oblako",
    )
    cluster = control_client.describe_clusters(ClusterIdentifier=cid)["Clusters"][0]
    assert cluster["NumberOfNodes"] == 3
    assert cluster["NodeType"] == "ra3.xlplus"
    assert cluster["Endpoint"]["Port"] == 5439
    control_client.delete_cluster(ClusterIdentifier=cid, SkipFinalClusterSnapshot=True)
