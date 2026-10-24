import boto3
import redshift_connector

redshift = boto3.client("redshift")


def connect(cluster_id="analytics"):
    found = redshift.describe_clusters(
        ClusterIdentifier=cluster_id
    )
    endpoint = found["Clusters"][0]["Endpoint"]
    conn = redshift_connector.connect(
        host=endpoint["Address"],
        port=endpoint["Port"],
        database="dev",
        user="admin",
        password="Analytics-local-1",
        ssl=True,
        sslmode="verify-ca",
    )
    conn.autocommit = True
    return conn


def query(sql):
    with connect().cursor() as cur:
        cur.execute(sql)
        return cur.fetchall() if cur.description else []
