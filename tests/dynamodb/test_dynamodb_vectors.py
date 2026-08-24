"""Integration test: DynamoDB native vector search over DynamoDB Local.

Requires Docker (DynamoDB Local). oblako runs a proxy in front of DynamoDB Local
that captures VectorIndexes and serves SearchVectors as brute-force KNN, and
grafts the new API onto boto3 so an unpatched client can call it. Unmodified
boto3 otherwise — CreateTable, PutItem, and SearchVectors all go through the one
dynamodb endpoint.
"""

import contextlib
import time

import pytest


@pytest.fixture(scope="module")
def ddb():
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker not available")
    from oblako.services import DynamoDBService

    try:
        client = DynamoDBService().get_vector_client()
    except Exception as err:  # noqa: BLE001
        pytest.skip(f"DynamoDB Local unavailable: {err}")
    return client


def _create(ddb, table):
    ddb.create_table(
        TableName=table,
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
        VectorIndexes=[
            {
                "IndexName": "by-embedding",
                "VectorAttribute": {"AttributeName": "embedding"},
                "Dimensions": 4,
                "DistanceFunction": "COSINE",
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    for _ in range(30):
        if ddb.describe_table(TableName=table)["Table"]["TableStatus"] == "ACTIVE":
            break
        time.sleep(0.2)


def _vec(values):
    return {"L": [{"N": str(v)} for v in values]}


def _query(values):
    # SearchVector elements are AttributeValues (a DynamoDB list of Numbers)
    return [{"N": str(v)} for v in values]


def test_search_vectors_returns_nearest_first(ddb):
    table = "vec-items"
    try:
        _create(ddb, table)

        # describe_table echoes the captured vector index
        desc = ddb.describe_table(TableName=table)["Table"]
        assert desc["VectorIndexes"][0]["IndexName"] == "by-embedding"

        rows = {
            "a": [1.0, 0.0, 0.0, 0.0],
            "b": [0.0, 1.0, 0.0, 0.0],
            "c": [0.9, 0.1, 0.0, 0.0],
        }
        for rid, vector in rows.items():
            ddb.put_item(
                TableName=table,
                Item={
                    "id": {"S": rid},
                    "title": {"S": f"item {rid}"},
                    "embedding": _vec(vector),
                },
            )

        resp = ddb.search_vectors(
            TableName=table,
            IndexName="by-embedding",
            SearchVector=_query([1.0, 0.0, 0.0, 0.0]),
            TopK=2,
        )
        results = resp["SearchResults"]
        assert [r["Item"]["id"]["S"] for r in results] == ["a", "c"]  # nearest first
        assert results[0]["Score"] > results[1]["Score"]  # a more similar than c

        # projection ALL returns the item minus the embedding (excluded by default)
        first = results[0]["Item"]
        assert first["title"]["S"] == "item a"
        assert "embedding" not in first
    finally:
        with contextlib.suppress(Exception):
            ddb.delete_table(TableName=table)  # best-effort cleanup


def test_euclidean_distance_orders_by_closeness(ddb):
    table = "vec-euclid"
    try:
        ddb.create_table(
            TableName=table,
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
            VectorIndexes=[
                {
                    "IndexName": "euclid",
                    "VectorAttribute": {"AttributeName": "v"},
                    "Dimensions": 2,
                    "DistanceFunction": "EUCLIDEAN",
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                }
            ],
        )
        for _ in range(30):
            if ddb.describe_table(TableName=table)["Table"]["TableStatus"] == "ACTIVE":
                break
            time.sleep(0.2)
        for rid, vector in {"near": [0.0, 0.0], "far": [10.0, 10.0]}.items():
            ddb.put_item(
                TableName=table, Item={"id": {"S": rid}, "v": _vec(vector)}
            )
        resp = ddb.search_vectors(
            TableName=table, IndexName="euclid", SearchVector=_query([0.1, 0.1]), TopK=2
        )
        ids = [r["Item"]["id"]["S"] for r in resp["SearchResults"]]
        assert ids == ["near", "far"]  # euclidean: smaller distance first
        # KEYS_ONLY projection returns only the key attribute
        assert set(resp["SearchResults"][0]["Item"]) == {"id"}
    finally:
        with contextlib.suppress(Exception):
            ddb.delete_table(TableName=table)
