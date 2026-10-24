import boto3
import psycopg

rds = boto3.client("rds")
USER, PASSWORD = "shop", "shop-local-1"


def describe(instance_id):
    found = rds.describe_db_instances(
        DBInstanceIdentifier=instance_id
    )
    return found["DBInstances"][0]


def wait(instance_id):
    waiter = rds.get_waiter("db_instance_available")
    waiter.wait(DBInstanceIdentifier=instance_id)
    return describe(instance_id)


def endpoint(instance_id):
    return describe(instance_id)["Endpoint"]


def connect(instance_id):
    e = endpoint(instance_id)
    return psycopg.connect(
        host=e["Address"],
        port=e["Port"],
        dbname="shop",
        user=USER,
        password=PASSWORD,
        autocommit=True,
    )


def one(conn, sql, params=None):
    # the first column of the first row
    return conn.execute(sql, params).fetchone()[0]
