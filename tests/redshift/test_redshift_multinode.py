"""Integration test: a multi-node Redshift cluster created through the Redshift API.

Requires moto on 5500 and Docker. ``create_cluster(ClusterType='multi-node')``
through the redshift-control proxy (started in-process) runs a Citus cluster: a
leader and NumberOfNodes compute nodes, from the redshift-cluster image (built
locally or pulled). DISTKEY tables shard across the compute nodes, the endpoint
checks passwords, and the Data API reaches the cluster by its identifier.
"""

import time

import boto3
import psycopg
import pytest

from oblako.engines import redshift_control, redshift_data
from tests.ports import free_port

CLUSTER = "pytest-multinode"
PASSWORD = "Secret123x"


@pytest.fixture(scope="module")
def redshift():
    client = redshift_control.get_client()

    def cleanup():
        try:
            client.delete_cluster(
                ClusterIdentifier=CLUSTER, SkipFinalClusterSnapshot=True
            )
        except client.exceptions.ClusterNotFoundFault:
            pass

    cleanup()
    yield client
    cleanup()


def _connect(endpoint, password=PASSWORD):
    return psycopg.connect(
        host=endpoint["Address"],
        port=endpoint["Port"],
        user="admin",
        password=password,
        dbname="dev",
        autocommit=True,
        sslmode="require",
    )


def test_multi_node_cluster(redshift):
    created = redshift.create_cluster(
        ClusterIdentifier=CLUSTER,
        ClusterType="multi-node",
        NodeType="ra3.large",
        NumberOfNodes=2,
        MasterUsername="admin",
        MasterUserPassword=PASSWORD,
        DBName="dev",
    )["Cluster"]
    assert created["ClusterStatus"] == "creating"
    redshift.get_waiter("cluster_available").wait(
        ClusterIdentifier=CLUSTER, WaiterConfig={"Delay": 2, "MaxAttempts": 300}
    )
    cluster = redshift.describe_clusters(ClusterIdentifier=CLUSTER)["Clusters"][0]
    endpoint = cluster["Endpoint"]
    assert endpoint["Address"] == f"{CLUSTER}.us-east-1.redshift.localhost"
    roles = [node["NodeRole"] for node in cluster["ClusterNodes"]]
    assert roles == ["LEADER", "COMPUTE-0", "COMPUTE-1"]

    with _connect(endpoint) as conn:
        conn.execute(
            "CREATE TABLE sales (id int, region varchar(16), amount numeric(10,2))"
            " DISTKEY(region) SORTKEY(id)"
        )
        conn.execute(
            "INSERT INTO sales SELECT g, 'r' || (g % 8), g * 1.5"
            " FROM generate_series(1, 10000) g"
        )
        assert conn.execute("SELECT count(*) FROM sales").fetchone() == (10000,)
        # distributed when CREATE TABLE returned: shards on both compute nodes
        shards = conn.execute(
            "SELECT nodename, count(*) FROM citus_shards GROUP BY 1 ORDER BY 1"
        ).fetchall()
        assert [name for name, _ in shards] == [
            f"{CLUSTER}-compute-0",
            f"{CLUSTER}-compute-1",
        ]
        conn.execute("CREATE TABLE regions (region varchar(16)) DISTSTYLE ALL")
        conn.execute("INSERT INTO regions VALUES ('r0'), ('r1')")
        info = dict(
            (row[0], row[1:])
            for row in conn.execute(
                'SELECT "table", diststyle, sortkey1, tbl_rows FROM svv_table_info'
                " WHERE \"table\" IN ('sales', 'regions')"
            ).fetchall()
        )
        assert info == {
            "sales": ("KEY(region)", "id", 10000),
            "regions": ("ALL", None, 2),
        }

    with pytest.raises(psycopg.OperationalError):
        _connect(endpoint, password="wrong")

    url = redshift_data.start_in_thread(port=free_port())
    data = boto3.client(
        "redshift-data",
        endpoint_url=url,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    statement = data.execute_statement(
        ClusterIdentifier=CLUSTER, Database="dev", Sql="SELECT count(*) FROM sales"
    )["Id"]
    for _ in range(50):
        if data.describe_statement(Id=statement)["Status"] == "FINISHED":
            break
        time.sleep(0.2)
    records = data.get_statement_result(Id=statement)["Records"]
    assert records == [[{"longValue": 10000}]]


def test_redshift_connector_ddl_is_distributed(redshift):
    """redshift_connector's two-round extended protocol still distributes DISTKEY."""
    redshift_connector = pytest.importorskip("redshift_connector")
    cluster = redshift.describe_clusters(ClusterIdentifier=CLUSTER)["Clusters"][0]
    conn = redshift_connector.connect(
        host=cluster["Endpoint"]["Address"],
        port=cluster["Endpoint"]["Port"],
        database="dev",
        user="admin",
        password=PASSWORD,
        ssl=False,  # CI has no `oblako trust` for the self-signed cert
    )
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE visits (id int, page varchar(32)) DISTKEY(page)")
        cur.execute("SELECT diststyle FROM svv_table_info WHERE \"table\" = 'visits'")
        assert cur.fetchone()[0] == "KEY(page)"
    finally:
        conn.close()


def test_single_node_cluster_is_the_shared_engine(redshift):
    redshift.create_cluster(
        ClusterIdentifier="pytest-single",
        ClusterType="single-node",
        NodeType="ra3.large",
        MasterUsername="admin",
        MasterUserPassword=PASSWORD,
    )
    try:
        cluster = redshift.describe_clusters(ClusterIdentifier="pytest-single")
        endpoint = cluster["Clusters"][0]["Endpoint"]
        assert endpoint == {"Address": "localhost", "Port": 5439}
    finally:
        redshift.delete_cluster(
            ClusterIdentifier="pytest-single", SkipFinalClusterSnapshot=True
        )
