"""SageMaker Feature Store: control plane + online/offline data planes.

The control plane (create/describe/list/delete_feature_group) and the online
store (put/get/delete/batch_get_record via sagemaker-featurestore-runtime) are
in-process, so those tests need no external services. The offline store writes
Parquet to S3, so that test needs S3Proxy (override OBLAKO_TEST_S3_ENDPOINT to
point at an isolated one). Unmodified boto3 throughout.
"""

import io
import os
import uuid

import boto3
import pytest
from botocore.config import Config

S3_ENDPOINT = os.environ.get("OBLAKO_TEST_S3_ENDPOINT", "http://localhost:9000")
BUCKET = "sm-fs-ci"
ROLE = "arn:aws:iam::000000000000:role/oblako"

DEFS = [
    {"FeatureName": "customer_id", "FeatureType": "String"},
    {"FeatureName": "age", "FeatureType": "Integral"},
    {"FeatureName": "score", "FeatureType": "Fractional"},
    {"FeatureName": "event_time", "FeatureType": "String"},
]


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
        ),
    )


@pytest.fixture(scope="module")
def clients():
    from oblako.services import SageMakerService

    svc = SageMakerService()
    return svc.get_client(), svc.get_featurestore_runtime_client()


def _record(cid, age, score):
    return [
        {"FeatureName": "customer_id", "ValueAsString": cid},
        {"FeatureName": "age", "ValueAsString": str(age)},
        {"FeatureName": "score", "ValueAsString": str(score)},
        {"FeatureName": "event_time", "ValueAsString": "2026-08-24T00:00:00Z"},
    ]


def test_control_plane_and_online_store(clients):
    sm, fs = clients
    fg = "fg-online"
    sm.create_feature_group(
        FeatureGroupName=fg,
        RecordIdentifierFeatureName="customer_id",
        EventTimeFeatureName="event_time",
        FeatureDefinitions=DEFS,
        OnlineStoreConfig={"EnableOnlineStore": True},
        RoleArn=ROLE,
    )
    desc = sm.describe_feature_group(FeatureGroupName=fg)
    assert desc["FeatureGroupStatus"] == "Created"
    assert desc["RecordIdentifierFeatureName"] == "customer_id"
    assert fg in [
        g["FeatureGroupName"] for g in sm.list_feature_groups()["FeatureGroupSummaries"]
    ]

    fs.put_record(FeatureGroupName=fg, Record=_record("c1", 30, 0.9))
    fs.put_record(FeatureGroupName=fg, Record=_record("c2", 41, 0.2))

    got = fs.get_record(FeatureGroupName=fg, RecordIdentifierValueAsString="c1")
    values = {f["FeatureName"]: f["ValueAsString"] for f in got["Record"]}
    assert values["age"] == "30" and values["score"] == "0.9"

    # feature projection
    got = fs.get_record(
        FeatureGroupName=fg,
        RecordIdentifierValueAsString="c1",
        FeatureNames=["score"],
    )
    assert [f["FeatureName"] for f in got["Record"]] == ["score"]

    # batch across identifiers
    batch = fs.batch_get_record(
        Identifiers=[
            {"FeatureGroupName": fg, "RecordIdentifiersValueAsString": ["c1", "c2"]}
        ]
    )
    assert {r["RecordIdentifierValueAsString"] for r in batch["Records"]} == {
        "c1",
        "c2",
    }

    # a missing record comes back with no Record member (not an error)
    miss = fs.get_record(FeatureGroupName=fg, RecordIdentifierValueAsString="nope")
    assert "Record" not in miss

    fs.delete_record(
        FeatureGroupName=fg,
        RecordIdentifierValueAsString="c1",
        EventTime="2026-08-24T00:00:00Z",
    )
    assert "Record" not in fs.get_record(
        FeatureGroupName=fg, RecordIdentifierValueAsString="c1"
    )

    sm.delete_feature_group(FeatureGroupName=fg)
    with pytest.raises(sm.exceptions.ClientError):
        sm.describe_feature_group(FeatureGroupName=fg)


def test_offline_store_writes_queryable_parquet(clients):
    try:
        _s3().list_buckets()
    except Exception:
        pytest.skip("S3Proxy not available")
    import pyarrow.parquet as pq

    os.environ["AWS_ENDPOINT_URL_S3"] = S3_ENDPOINT
    sm, fs = clients
    s3 = _s3()
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.ClientError:
        pass

    # unique group so the offline data prefix is clean regardless of prior runs
    fg = f"fg-offline-{uuid.uuid4().hex[:8]}"
    sm.create_feature_group(
        FeatureGroupName=fg,
        RecordIdentifierFeatureName="customer_id",
        EventTimeFeatureName="event_time",
        FeatureDefinitions=DEFS,
        OnlineStoreConfig={"EnableOnlineStore": True},
        OfflineStoreConfig={"S3StorageConfig": {"S3Uri": f"s3://{BUCKET}/offline"}},
        RoleArn=ROLE,
    )
    fs.put_record(FeatureGroupName=fg, Record=_record("c1", 30, 0.9))
    fs.put_record(FeatureGroupName=fg, Record=_record("c2", 41, 0.2))

    objs = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"offline/{fg}/data/").get(
        "Contents", []
    )
    assert len(objs) == 2  # one Parquet per record
    rows = []
    for obj in objs:
        body = s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()
        table = pq.read_table(io.BytesIO(body))
        # typed columns per FeatureDefinitions
        assert table.schema.field("age").type == "int64"
        assert table.schema.field("score").type == "double"
        rows.append(table.to_pylist()[0])
    by_id = {r["customer_id"]: r for r in rows}
    assert by_id["c1"]["age"] == 30 and by_id["c1"]["score"] == 0.9
    assert by_id["c2"]["age"] == 41
