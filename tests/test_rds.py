"""Integration tests for RDS / Aurora.

Requires running services:
    docker compose up -d rds moto   # postgres engine on 5432, moto on 5500

Control plane (RDS instances + Aurora clusters) via boto3 'rds' against moto;
data plane (real SQL) via psycopg2 against the postgres engine.
"""

import boto3
import psycopg2
import pytest

from oblako.services import RdsService

CREDS = dict(region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
PG = dict(host="localhost", port=5432, user="oblako", password="oblako", dbname="oblako")


@pytest.fixture(scope="module")
def rds():
    return boto3.client("rds", endpoint_url="http://localhost:5500", **CREDS)


@pytest.fixture
def cursor():
    conn = psycopg2.connect(**PG)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS rds_test")
    cur.execute("CREATE TABLE rds_test (id INT PRIMARY KEY, name TEXT)")
    yield cur
    cur.execute("DROP TABLE IF EXISTS rds_test")
    cur.close()
    conn.close()


# -- control plane ---------------------------------------------------------
def test_create_db_instance(rds):
    cid = "pytest-rds-pg"
    try:
        rds.delete_db_instance(DBInstanceIdentifier=cid, SkipFinalSnapshot=True)
    except Exception:
        pass
    rds.create_db_instance(
        DBInstanceIdentifier=cid, Engine="postgres", DBInstanceClass="db.t3.micro",
        MasterUsername="oblako", MasterUserPassword="Oblako123", AllocatedStorage=20, DBName="oblako",
    )
    inst = rds.describe_db_instances(DBInstanceIdentifier=cid)["DBInstances"][0]
    assert inst["Engine"] == "postgres"
    assert inst["DBInstanceClass"] == "db.t3.micro"
    assert inst["Endpoint"]["Port"] == 5432
    rds.delete_db_instance(DBInstanceIdentifier=cid, SkipFinalSnapshot=True)


def test_create_aurora_cluster(rds):
    cid = "pytest-aurora"

    def teardown():
        # Aurora: instances must be deleted before the cluster.
        try:
            rds.delete_db_instance(DBInstanceIdentifier=f"{cid}-1", SkipFinalSnapshot=True)
        except Exception:
            pass
        try:
            rds.delete_db_cluster(DBClusterIdentifier=cid, SkipFinalSnapshot=True)
        except Exception:
            pass

    teardown()
    rds.create_db_cluster(
        DBClusterIdentifier=cid, Engine="aurora-postgresql",
        MasterUsername="oblako", MasterUserPassword="Oblako123", DatabaseName="oblako",
    )
    rds.create_db_instance(
        DBInstanceIdentifier=f"{cid}-1", DBClusterIdentifier=cid,
        Engine="aurora-postgresql", DBInstanceClass="db.r6g.large",
    )
    cluster = rds.describe_db_clusters(DBClusterIdentifier=cid)["DBClusters"][0]
    assert cluster["Engine"] == "aurora-postgresql"
    assert cluster.get("Endpoint")        # writer endpoint
    assert cluster.get("ReaderEndpoint")  # reader endpoint
    assert any(m["DBInstanceIdentifier"] == f"{cid}-1" for m in cluster.get("DBClusterMembers", []))
    teardown()


# -- data plane ------------------------------------------------------------
def test_connect_and_crud(cursor):
    cursor.executemany("INSERT INTO rds_test VALUES (%s, %s)", [(1, "alice"), (2, "bob")])
    cursor.execute("SELECT name FROM rds_test ORDER BY id")
    assert [r[0] for r in cursor.fetchall()] == ["alice", "bob"]


# -- seed helper -----------------------------------------------------------
def test_seed_idempotent(rds):
    svc = RdsService()  # control plane only (moto); no engine container needed
    spec = dict(
        instances=[{
            "DBInstanceIdentifier": "seed-inst", "Engine": "postgres",
            "DBInstanceClass": "db.t3.micro", "MasterUsername": "oblako",
            "MasterUserPassword": "Oblako123", "AllocatedStorage": 20, "DBName": "oblako",
        }],
        clusters=[{
            "DBClusterIdentifier": "seed-clus", "Engine": "aurora-postgresql",
            "MasterUsername": "oblako", "MasterUserPassword": "Oblako123", "DatabaseName": "oblako",
            "instances": [{
                "DBInstanceIdentifier": "seed-clus-1", "Engine": "aurora-postgresql",
                "DBInstanceClass": "db.r6g.large",
            }],
        }],
    )

    def cleanup():
        for fn, kw in [
            (rds.delete_db_instance, {"DBInstanceIdentifier": "seed-clus-1", "SkipFinalSnapshot": True}),
            (rds.delete_db_cluster, {"DBClusterIdentifier": "seed-clus", "SkipFinalSnapshot": True}),
            (rds.delete_db_instance, {"DBInstanceIdentifier": "seed-inst", "SkipFinalSnapshot": True}),
        ]:
            try:
                fn(**kw)
            except Exception:
                pass

    cleanup()
    try:
        first = svc.seed(**spec)
        assert "seed-inst" in first["instances"]
        assert "seed-clus" in first["clusters"]
        assert "seed-clus-1" in first["instances"]
        # idempotent: a second seed creates nothing new
        second = svc.seed(**spec)
        assert second == {"clusters": [], "instances": []}
        # and they really exist
        assert rds.describe_db_instances(DBInstanceIdentifier="seed-inst")["DBInstances"]
        assert rds.describe_db_clusters(DBClusterIdentifier="seed-clus")["DBClusters"]
    finally:
        cleanup()


# -- MySQL engine (skipped unless a mysql engine is running on 3306) -------
def test_mysql_connect_and_crud():
    pytest.importorskip("pymysql")
    svc = RdsService(engine="mysql")
    try:
        conn = svc.connect()
    except Exception:
        pytest.skip("MySQL engine not running on 3306")
    try:
        conn.autocommit(True)
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS rds_mysql_test")
        cur.execute("CREATE TABLE rds_mysql_test (id INT PRIMARY KEY, name TEXT)")
        cur.executemany("INSERT INTO rds_mysql_test VALUES (%s, %s)", [(1, "alice"), (2, "bob")])
        cur.execute("SELECT name FROM rds_mysql_test ORDER BY id")
        assert [r[0] for r in cur.fetchall()] == ["alice", "bob"]
        cur.execute("DROP TABLE rds_mysql_test")
        cur.close()
    finally:
        conn.close()
