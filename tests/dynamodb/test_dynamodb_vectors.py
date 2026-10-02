"""Integration test: DynamoDB native vector search over DynamoDB Local.

Requires Docker (DynamoDB Local). oblako runs a proxy in front of DynamoDB Local
that captures VectorIndexes and serves SearchVectors as brute-force KNN, with the
released API's search schema, condition and projection rules, and adds tagging.
Plain boto3 (1.43.64+): CreateTable, PutItem, and SearchVectors all go through
the one dynamodb endpoint.
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
    except Exception as err:
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
        # COSINE score is a distance (0 = identical): a is exact, so ~0 and smallest
        assert results[0]["Score"] < results[1]["Score"]
        assert results[0]["Score"] == pytest.approx(0.0, abs=1e-9)

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
            ddb.put_item(TableName=table, Item={"id": {"S": rid}, "v": _vec(vector)})
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


# ---------------------------------------------------------------------------
# Search schema, conditions, projections, write validation, tags
# ---------------------------------------------------------------------------
PRODUCTS = "vec-products"


@pytest.fixture
def products(ddb):
    with contextlib.suppress(Exception):
        ddb.delete_table(TableName=PRODUCTS)
    ddb.create_table(
        TableName=PRODUCTS,
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "category", "AttributeType": "S"},
            {"AttributeName": "brand", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
        VectorIndexes=[
            {
                "IndexName": "by-category",
                "VectorAttribute": {"AttributeName": "embedding"},
                "SearchSchema": [
                    {"AttributeName": "category", "SearchSchemaElementType": "HASH"},
                    {
                        "AttributeName": "brand",
                        "SearchSchemaElementType": "INLINE_FILTER",
                    },
                ],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": ["title"],
                },
                "Dimensions": 2,
                "DistanceFunction": "COSINE",
            }
        ],
        Tags=[{"Key": "team", "Value": "search"}],
    )
    rows = [
        ("p1", "audio", "acme", [1.0, 0.0]),
        ("p2", "audio", "other", [0.9, 0.1]),
        ("p3", "video", "acme", [1.0, 0.0]),
    ]
    for pid, category, brand, vector in rows:
        ddb.put_item(
            TableName=PRODUCTS,
            Item={
                "id": {"S": pid},
                "category": {"S": category},
                "brand": {"S": brand},
                "title": {"S": f"product {pid}"},
                "price": {"N": "10"},
                "embedding": _vec(vector),
            },
        )
    # no partition key: stored, but not in the vector index
    ddb.put_item(
        TableName=PRODUCTS,
        Item={"id": {"S": "p4"}, "embedding": _vec([1.0, 0.0])},
    )
    yield PRODUCTS
    with contextlib.suppress(Exception):
        ddb.delete_table(TableName=PRODUCTS)


def _search(ddb, condition, values, **extra):
    return ddb.search_vectors(
        TableName=PRODUCTS,
        IndexName="by-category",
        SearchVector=_query([1.0, 0.0]),
        TopK=10,
        SearchConditionExpression=condition,
        ExpressionAttributeValues=values,
        **extra,
    )["SearchResults"]


def test_describe_reports_search_schema_and_definitions(ddb, products):
    table = ddb.describe_table(TableName=products)["Table"]
    index = table["VectorIndexes"][0]
    assert [e["AttributeName"] for e in index["SearchSchema"]] == ["category", "brand"]
    assert index["IndexStatus"] == "ACTIVE"
    assert index["IndexArn"].endswith(f"table/{products}/index/by-category")
    names = {a["AttributeName"] for a in table["AttributeDefinitions"]}
    assert names == {"id", "category", "brand"}


def test_partition_key_scopes_the_search(ddb, products):
    results = _search(ddb, "category = :c", {":c": {"S": "audio"}})
    assert [r["Item"]["id"]["S"] for r in results] == ["p1", "p2"]


def test_inline_filter_and_name_placeholders(ddb, products):
    results = _search(
        ddb,
        "#c = :c AND brand = :b",
        {":c": {"S": "audio"}, ":b": {"S": "other"}},
        ExpressionAttributeNames={"#c": "category"},
    )
    assert [r["Item"]["id"]["S"] for r in results] == ["p2"]


def test_partition_key_is_required(ddb, products):
    with pytest.raises(ddb.exceptions.ClientError, match="partition key"):
        _search(ddb, "brand = :b", {":b": {"S": "acme"}})


def test_only_equality_is_supported(ddb, products):
    with pytest.raises(ddb.exceptions.ClientError, match="equality"):
        _search(ddb, "category <> :c", {":c": {"S": "audio"}})


def test_projection_and_vector_by_request_only(ddb, products):
    results = _search(ddb, "category = :c", {":c": {"S": "video"}})
    # INCLUDE: keys, search schema and title; price is not projected and the
    # vector is left out unless asked for
    assert set(results[0]["Item"]) == {"id", "category", "brand", "title"}
    results = _search(
        ddb,
        "category = :c",
        {":c": {"S": "video"}},
        ProjectionExpression="id, embedding, price",
    )
    assert set(results[0]["Item"]) == {"id", "embedding"}


def test_query_dimensions_and_top_k_are_checked(ddb, products):
    with pytest.raises(ddb.exceptions.ClientError, match="dimensions"):
        ddb.search_vectors(
            TableName=products,
            IndexName="by-category",
            SearchVector=_query([1.0, 0.0, 0.0]),
            TopK=1,
            SearchConditionExpression="category = :c",
            ExpressionAttributeValues={":c": {"S": "audio"}},
        )


def test_wrong_vector_length_is_rejected_on_write(ddb, products):
    with pytest.raises(ddb.exceptions.ClientError, match="list of 2 numbers"):
        ddb.put_item(
            TableName=products,
            Item={
                "id": {"S": "bad"},
                "category": {"S": "audio"},
                "embedding": _vec([1.0, 0.0, 0.0]),
            },
        )


def test_tags(ddb, products):
    arn = ddb.describe_table(TableName=products)["Table"]["TableArn"]
    assert ddb.list_tags_of_resource(ResourceArn=arn)["Tags"] == [
        {"Key": "team", "Value": "search"}
    ]
    ddb.tag_resource(ResourceArn=arn, Tags=[{"Key": "env", "Value": "dev"}])
    ddb.untag_resource(ResourceArn=arn, TagKeys=["team"])
    assert ddb.list_tags_of_resource(ResourceArn=arn)["Tags"] == [
        {"Key": "env", "Value": "dev"}
    ]


def test_add_and_delete_an_index_with_update_table(ddb, products):
    ddb.update_table(
        TableName=products,
        VectorIndexUpdates=[
            {
                "Create": {
                    "IndexName": "all-products",
                    "VectorAttribute": {"AttributeName": "embedding"},
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                    "Dimensions": 2,
                    "DistanceFunction": "EUCLIDEAN",
                }
            }
        ],
    )
    results = ddb.search_vectors(
        TableName=products,
        IndexName="all-products",
        SearchVector=_query([1.0, 0.0]),
        TopK=10,
    )["SearchResults"]
    assert len(results) == 4  # no partition key: every item with a vector
    assert set(results[0]["Item"]) == {"id"}
    ddb.update_table(
        TableName=products,
        VectorIndexUpdates=[{"Delete": {"IndexName": "all-products"}}],
    )
    indexes = ddb.describe_table(TableName=products)["Table"]["VectorIndexes"]
    assert [i["IndexName"] for i in indexes] == ["by-category"]


def test_one_index_change_per_update(ddb, products):
    create = {
        "VectorAttribute": {"AttributeName": "embedding"},
        "Projection": {"ProjectionType": "KEYS_ONLY"},
        "Dimensions": 2,
        "DistanceFunction": "COSINE",
    }
    with pytest.raises(ddb.exceptions.LimitExceededException):
        ddb.update_table(
            TableName=products,
            VectorIndexUpdates=[
                {"Create": {"IndexName": "idx-a1", **create}},
                {"Create": {"IndexName": "idx-a2", **create}},
            ],
        )
