"""Integration tests for OpenSearch (requires: docker compose up opensearch)."""


import pytest
from opensearchpy import OpenSearch

OS_CONFIG = dict(hosts=[{"host": "localhost", "port": 9200}], use_ssl=False)
INDEX_NAME = "test-oblako-vectors"


@pytest.fixture
def client():
    return OpenSearch(**OS_CONFIG)


@pytest.fixture
def index(client):
    if client.indices.exists(index=INDEX_NAME):
        client.indices.delete(index=INDEX_NAME)
    client.indices.create(
        index=INDEX_NAME,
        body={
            "settings": {
                "index": {"knn": True},
            },
            "mappings": {
                "properties": {
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": 4,
                        "method": {"name": "hnsw", "engine": "faiss"},
                    },
                    "text": {"type": "text"},
                }
            },
        },
    )
    yield INDEX_NAME
    client.indices.delete(index=INDEX_NAME)


def test_index_and_search_document(client, index):
    client.index(index=index, id="1", body={"text": "credit risk model", "embedding": [0.1, 0.2, 0.3, 0.4]})
    client.index(index=index, id="2", body={"text": "fraud detection", "embedding": [0.9, 0.8, 0.7, 0.6]})
    client.indices.refresh(index=index)

    results = client.search(
        index=index,
        body={
            "query": {
                "knn": {
                    "embedding": {
                        "vector": [0.1, 0.2, 0.3, 0.4],
                        "k": 1,
                    }
                }
            }
        },
    )
    hits = results["hits"]["hits"]
    assert len(hits) >= 1
    assert hits[0]["_source"]["text"] == "credit risk model"


def test_text_search(client, index):
    client.index(index=index, id="1", body={"text": "loan default prediction", "embedding": [0.1, 0.2, 0.3, 0.4]})
    client.index(index=index, id="2", body={"text": "customer churn analysis", "embedding": [0.5, 0.6, 0.7, 0.8]})
    client.indices.refresh(index=index)

    results = client.search(index=index, body={"query": {"match": {"text": "loan"}}})
    assert results["hits"]["total"]["value"] == 1
    assert results["hits"]["hits"][0]["_source"]["text"] == "loan default prediction"
