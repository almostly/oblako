"""Unit tests for the Redshift control proxy's response rewriting (no Docker)."""

from oblako.engines.redshift_control.app import (
    MAX_NODES,
    RedshiftControlProxy,
    region_of,
    rewrite,
)

_CLUSTER = (
    "<Cluster><ClusterIdentifier>{id}</ClusterIdentifier>"
    "<ClusterStatus>available</ClusterStatus>"
    "<Endpoint><Address>{id}.abc.us-east-1.redshift.amazonaws.com</Address>"
    "<Port>5439</Port></Endpoint><NumberOfNodes>{nodes}</NumberOfNodes></Cluster>"
)


def _response(*clusters):
    return (
        '<DescribeClustersResponse xmlns="http://redshift.amazonaws.com/doc/2012-12-01/">'
        f"<DescribeClustersResult><Clusters>{''.join(clusters)}</Clusters>"
        "</DescribeClustersResult></DescribeClustersResponse>"
    )


def test_single_node_cluster_points_at_the_shared_engine():
    out = rewrite(_response(_CLUSTER.format(id="solo", nodes=1)), {})
    assert "<Address>localhost</Address><Port>5439</Port>" in out


def test_multi_node_cluster_gets_its_endpoint_status_and_nodes():
    record = {
        "region": "eu-west-1",
        "port": 51234,
        "status": "creating",
        "node_ips": {"LEADER": "10.0.0.4", "COMPUTE-0": "10.0.0.2"},
    }
    out = rewrite(_response(_CLUSTER.format(id="big", nodes=2)), {"big": record})
    assert "<Address>big.eu-west-1.redshift.localhost</Address>" in out
    assert "<Port>51234</Port>" in out
    assert "<ClusterStatus>creating</ClusterStatus>" in out
    assert "<NodeRole>LEADER</NodeRole><PrivateIPAddress>10.0.0.4" in out
    assert "<NodeRole>COMPUTE-0</NodeRole>" in out


def test_multi_node_cluster_oblako_does_not_run_is_unchanged():
    xml = _response(_CLUSTER.format(id="meta", nodes=3))
    assert rewrite(xml, {}) == xml


def test_too_many_nodes_is_refused():
    proxy = RedshiftControlProxy("http://localhost:1")
    form = {
        "ClusterType": "multi-node",
        "NumberOfNodes": str(MAX_NODES + 1),
        "ClusterIdentifier": "huge",
    }
    refused = proxy._precheck("CreateCluster", form)
    assert refused is not None and b"InvalidParameterValue" in refused.body


def test_region_from_the_sigv4_scope():
    auth = "AWS4-HMAC-SHA256 Credential=t/20261003/ap-south-1/redshift/aws4_request"
    assert region_of({"authorization": auth}) == "ap-south-1"
