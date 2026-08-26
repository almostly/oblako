"""Tests for the local S3 Vectors (`s3vectors`) engine.

Unmodified boto3 `s3vectors` calls hit the in-process engine: vector buckets,
indexes, PutVectors, brute-force k-NN QueryVectors, metadata filters, and
Get/List/DeleteVectors. No Docker needed (pure in-process).
"""

import boto3
import pytest

from oblako.engines import s3vectors


@pytest.fixture
def client():
    s3vectors.reset()
    url = s3vectors.start_in_thread()
    return boto3.client(
        "s3vectors",
        endpoint_url=url,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture
def index(client):
    client.create_vector_bucket(vectorBucketName="docs")
    client.create_index(
        vectorBucketName="docs",
        indexName="emb",
        dataType="float32",
        dimension=3,
        distanceMetric="cosine",
    )
    client.put_vectors(
        vectorBucketName="docs",
        indexName="emb",
        vectors=[
            {"key": "a", "data": {"float32": [1.0, 0.0, 0.0]}, "metadata": {"lang": "en", "n": 1}},
            {"key": "b", "data": {"float32": [0.0, 1.0, 0.0]}, "metadata": {"lang": "en", "n": 2}},
            {"key": "c", "data": {"float32": [0.9, 0.1, 0.0]}, "metadata": {"lang": "fr", "n": 3}},
        ],
    )
    return client


def test_bucket_and_index_lifecycle(client):
    arn = client.create_vector_bucket(vectorBucketName="docs")["vectorBucketArn"]
    assert arn.endswith(":bucket/docs")
    client.create_index(
        vectorBucketName="docs", indexName="emb",
        dataType="float32", dimension=4, distanceMetric="euclidean",
    )
    assert [b["vectorBucketName"] for b in client.list_vector_buckets()["vectorBuckets"]] == ["docs"]
    idx = client.get_index(vectorBucketName="docs", indexName="emb")["index"]
    assert idx["dimension"] == 4 and idx["distanceMetric"] == "euclidean"
    assert [i["indexName"] for i in client.list_indexes(vectorBucketName="docs")["indexes"]] == ["emb"]


def test_query_returns_nearest_with_distance(index):
    res = index.query_vectors(
        vectorBucketName="docs", indexName="emb", topK=2,
        queryVector={"float32": [1.0, 0.05, 0.0]},
        returnMetadata=True, returnDistance=True,
    )
    assert res["distanceMetric"] == "cosine"
    keys = [r["key"] for r in res["vectors"]]
    assert keys == ["a", "c"]  # nearest first
    assert res["vectors"][0]["distance"] < res["vectors"][1]["distance"]
    assert res["vectors"][0]["metadata"]["lang"] == "en"


def test_query_metadata_filter_equality_and_operator(index):
    en = index.query_vectors(
        vectorBucketName="docs", indexName="emb", topK=5,
        queryVector={"float32": [1.0, 0.0, 0.0]}, filter={"lang": "en"},
    )
    assert sorted(r["key"] for r in en["vectors"]) == ["a", "b"]
    ge2 = index.query_vectors(
        vectorBucketName="docs", indexName="emb", topK=5,
        queryVector={"float32": [1.0, 0.0, 0.0]}, filter={"n": {"$gte": 2}},
    )
    assert sorted(r["key"] for r in ge2["vectors"]) == ["b", "c"]


def test_get_list_and_delete_vectors(index):
    got = index.get_vectors(
        vectorBucketName="docs", indexName="emb", keys=["a", "c"],
        returnData=True, returnMetadata=True,
    )["vectors"]
    by_key = {r["key"]: r for r in got}
    assert by_key["a"]["data"]["float32"] == [1.0, 0.0, 0.0]
    assert by_key["c"]["metadata"]["lang"] == "fr"

    index.delete_vectors(vectorBucketName="docs", indexName="emb", keys=["a"])
    remaining = index.list_vectors(vectorBucketName="docs", indexName="emb")["vectors"]
    assert sorted(r["key"] for r in remaining) == ["b", "c"]


def test_dimension_mismatch_is_rejected(index):
    with pytest.raises(Exception) as excinfo:  # botocore ClientError
        index.put_vectors(
            vectorBucketName="docs", indexName="emb",
            vectors=[{"key": "x", "data": {"float32": [1.0, 2.0]}}],
        )
    assert "dimension" in str(excinfo.value).lower()


def test_query_by_index_arn(client):
    client.create_vector_bucket(vectorBucketName="vec")
    arn = client.create_index(
        vectorBucketName="vec", indexName="idx",
        dataType="float32", dimension=2, distanceMetric="cosine",
    )["indexArn"]
    client.put_vectors(indexArn=arn, vectors=[{"key": "k", "data": {"float32": [1.0, 1.0]}}])
    res = client.query_vectors(indexArn=arn, topK=1, queryVector={"float32": [1.0, 1.0]})
    assert [r["key"] for r in res["vectors"]] == ["k"]


def test_notebook_env_exposes_s3vectors():
    from oblako.notebook import ENDPOINTS

    assert "AWS_ENDPOINT_URL_S3VECTORS" in ENDPOINTS
