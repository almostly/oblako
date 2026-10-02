"""Unit tests for the rds-control proxy's response rewriting (no Docker)."""

from oblako.engines.rds_control.app import region_of, rewrite

_RESPONSE = (
    '<DescribeDBInstancesResponse xmlns="http://rds.amazonaws.com/doc/2014-10-31/">'
    "<DescribeDBInstancesResult><DBInstances>"
    "<DBInstance><DBInstanceIdentifier>{a}</DBInstanceIdentifier>"
    "<DBInstanceStatus>available</DBInstanceStatus><EngineVersion>16</EngineVersion>"
    "<Endpoint><Address>{a}.aaaaaaaaaa.us-east-1.rds.amazonaws.com</Address>"
    "<Port>5432</Port></Endpoint>"
    "<ReadReplicaDBInstanceIdentifiers>"
    "<ReadReplicaDBInstanceIdentifier>{b}</ReadReplicaDBInstanceIdentifier>"
    "</ReadReplicaDBInstanceIdentifiers></DBInstance>"
    "<DBInstance><DBInstanceIdentifier>{b}</DBInstanceIdentifier>"
    "<DBInstanceStatus>available</DBInstanceStatus>"
    "<Endpoint><Address>{b}.aaaaaaaaaa.us-east-1.rds.amazonaws.com</Address>"
    "<Port>5432</Port></Endpoint>"
    "<ReadReplicaSourceDBInstanceIdentifier>{a}</ReadReplicaSourceDBInstanceIdentifier>"
    "<ReadReplicaDBInstanceIdentifiers/></DBInstance>"
    "</DBInstances></DescribeDBInstancesResult></DescribeDBInstancesResponse>"
)


def _record(port, status="available", promoted=False):
    return {
        "port": port,
        "region": "eu-west-1",
        "status": status,
        "promoted": promoted,
        "engine_version": "16.13",
    }


def test_rewrite_points_endpoints_at_the_containers():
    xml = _RESPONSE.format(a="src", b="rep")
    out = rewrite(xml, {"src": _record(50001), "rep": _record(50002, "creating")})
    assert "<Address>src.eu-west-1.rds.localhost</Address><Port>50001</Port>" in out
    assert "<Address>rep.eu-west-1.rds.localhost</Address><Port>50002</Port>" in out
    assert "<DBInstanceStatus>creating</DBInstanceStatus>" in out
    assert "rds.amazonaws.com</Address>" not in out
    assert "<EngineVersion>16.13</EngineVersion>" in out
    # an unpromoted replica keeps its source, and the source lists it
    assert "<ReadReplicaSourceDBInstanceIdentifier>src<" in out
    assert "<ReadReplicaDBInstanceIdentifier>rep<" in out


def test_rewrite_detaches_a_promoted_replica():
    xml = _RESPONSE.format(a="src", b="rep")
    out = rewrite(xml, {"src": _record(50001), "rep": _record(50002, promoted=True)})
    assert "ReadReplicaSourceDBInstanceIdentifier" not in out
    assert "<ReadReplicaDBInstanceIdentifier>rep<" not in out


def test_rewrite_leaves_instances_oblako_does_not_run():
    xml = _RESPONSE.format(a="aurora-1", b="aurora-2")
    assert rewrite(xml, {"other": _record(50001)}) == xml
    assert rewrite(xml, {}) == xml


def test_region_from_the_sigv4_scope():
    auth = (
        "AWS4-HMAC-SHA256 Credential=test/20261002/eu-central-1/rds/aws4_request, "
        "SignedHeaders=host, Signature=abc"
    )
    assert region_of({"authorization": auth}) == "eu-central-1"
