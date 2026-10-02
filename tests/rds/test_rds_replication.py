"""Integration tests for RDS instances, read replicas and logical replication.

Requires moto on 5500 and Docker: the rds-control proxy (started in-process here)
runs one PostgreSQL container per DB instance, each reached at its own endpoint,
``<id>.<region>.rds.localhost:<port>``.
"""

import time

import psycopg
import pytest

from oblako.engines import rds_control

PASSWORD = "Secret123"
GROUP = "pytest-logical"
IDS = ("pytest-sub", "pytest-rep", "pytest-pub", "pytest-nobackup")


@pytest.fixture(scope="module")
def rds():
    client = rds_control.get_client()

    def cleanup():
        for iid in IDS:
            try:
                client.delete_db_instance(
                    DBInstanceIdentifier=iid, SkipFinalSnapshot=True
                )
            except client.exceptions.DBInstanceNotFoundFault:
                pass
        try:
            client.delete_db_parameter_group(DBParameterGroupName=GROUP)
        except client.exceptions.DBParameterGroupNotFoundFault:
            pass

    cleanup()
    yield client
    cleanup()


def _create(rds, iid, **extra):
    return rds.create_db_instance(
        DBInstanceIdentifier=iid,
        Engine="postgres",
        DBInstanceClass="db.t3.micro",
        MasterUsername="app",
        MasterUserPassword=PASSWORD,
        AllocatedStorage=20,
        DBName="shop",
        **extra,
    )["DBInstance"]


def _wait(rds, iid):
    rds.get_waiter("db_instance_available").wait(
        DBInstanceIdentifier=iid, WaiterConfig={"Delay": 1, "MaxAttempts": 240}
    )
    return rds.describe_db_instances(DBInstanceIdentifier=iid)["DBInstances"][0]


def _connect(instance):
    endpoint = instance["Endpoint"]
    return psycopg.connect(
        host=endpoint["Address"],
        port=endpoint["Port"],
        user="app",
        password=PASSWORD,
        dbname="shop",
        autocommit=True,
    )


def _eventually(fn, expected, timeout=15.0):
    deadline = time.time() + timeout
    while True:
        value = fn()
        if value == expected or time.time() > deadline:
            return value
        time.sleep(0.3)


def test_instance_replica_logical_and_promotion(rds):
    created = _create(rds, "pytest-pub")
    assert created["DBInstanceStatus"] == "creating"
    assert created["Endpoint"]["Address"] == "pytest-pub.us-east-1.rds.localhost"
    pub = _wait(rds, "pytest-pub")
    with _connect(pub) as conn:
        conn.execute("CREATE TABLE orders (id int PRIMARY KEY, total numeric)")
        conn.execute(
            "INSERT INTO orders SELECT g, g * 1.5 FROM generate_series(1, 100) g"
        )
        assert conn.execute("SHOW wal_level").fetchone() == ("replica",)

    # A read replica is a streaming standby: it sees new writes and refuses its own.
    replica = rds.create_db_instance_read_replica(
        DBInstanceIdentifier="pytest-rep", SourceDBInstanceIdentifier="pytest-pub"
    )["DBInstance"]
    assert replica["ReadReplicaSourceDBInstanceIdentifier"] == "pytest-pub"
    rep = _wait(rds, "pytest-rep")
    with _connect(pub) as conn:
        conn.execute("INSERT INTO orders VALUES (101, 7)")
        states = conn.execute("SELECT state FROM pg_stat_replication").fetchall()
        assert states == [("streaming",)]
    with _connect(rep) as conn:
        count = _eventually(
            lambda: conn.execute("SELECT count(*) FROM orders").fetchone()[0], 101
        )
        assert count == 101
        assert conn.execute("SELECT pg_is_in_recovery()").fetchone() == (True,)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO orders VALUES (999, 1)")

    # rds.logical_replication = 1 takes effect on reboot, as on RDS.
    rds.create_db_parameter_group(
        DBParameterGroupName=GROUP,
        DBParameterGroupFamily="postgres16",
        Description="logical replication",
    )
    rds.modify_db_parameter_group(
        DBParameterGroupName=GROUP,
        Parameters=[
            {
                "ParameterName": "rds.logical_replication",
                "ParameterValue": "1",
                "ApplyMethod": "pending-reboot",
            }
        ],
    )
    rds.modify_db_instance(
        DBInstanceIdentifier="pytest-pub",
        DBParameterGroupName=GROUP,
        ApplyImmediately=True,
    )
    rds.reboot_db_instance(DBInstanceIdentifier="pytest-pub")
    status = rds.describe_db_instances(DBInstanceIdentifier="pytest-pub")
    assert status["DBInstances"][0]["DBInstanceStatus"] == "rebooting"
    pub = _wait(rds, "pytest-pub")
    _create(rds, "pytest-sub")
    sub = _wait(rds, "pytest-sub")
    with _connect(pub) as conn:
        assert conn.execute("SHOW wal_level").fetchone() == ("logical",)
        conn.execute("CREATE PUBLICATION orders_pub FOR TABLE orders")
    address, port = pub["Endpoint"]["Address"], pub["Endpoint"]["Port"]
    dsn = f"host={address} port={port} user=app password={PASSWORD} dbname=shop"
    with _connect(sub) as conn:
        conn.execute("CREATE TABLE orders (id int PRIMARY KEY, total numeric)")
        conn.execute(
            f"CREATE SUBSCRIPTION orders_sub CONNECTION '{dsn}' PUBLICATION orders_pub"
        )
        count = _eventually(
            lambda: conn.execute("SELECT count(*) FROM orders").fetchone()[0], 101
        )
        assert count == 101
        with _connect(pub) as source:
            source.execute("INSERT INTO orders VALUES (102, 9)")
        count = _eventually(
            lambda: conn.execute("SELECT count(*) FROM orders").fetchone()[0], 102
        )
        assert count == 102
        conn.execute("DROP SUBSCRIPTION orders_sub")

    # Promotion turns the standby into a standalone, writable instance.
    rds.promote_read_replica(DBInstanceIdentifier="pytest-rep")
    rep = _wait(rds, "pytest-rep")
    assert "ReadReplicaSourceDBInstanceIdentifier" not in rep
    source = rds.describe_db_instances(DBInstanceIdentifier="pytest-pub")
    assert source["DBInstances"][0]["ReadReplicaDBInstanceIdentifiers"] == []
    with _connect(rep) as conn:
        assert conn.execute("SELECT pg_is_in_recovery()").fetchone() == (False,)
        conn.execute("INSERT INTO orders VALUES (500, 1)")


def test_replica_needs_automated_backups_on_the_source(rds):
    _create(rds, "pytest-nobackup", BackupRetentionPeriod=0)
    try:
        _wait(rds, "pytest-nobackup")
        with pytest.raises(rds.exceptions.InvalidDBInstanceStateFault):
            rds.create_db_instance_read_replica(
                DBInstanceIdentifier="pytest-nobackup-rep",
                SourceDBInstanceIdentifier="pytest-nobackup",
            )
    finally:
        rds.delete_db_instance(
            DBInstanceIdentifier="pytest-nobackup", SkipFinalSnapshot=True
        )
