"""DynamoDB native vector search on oblako, fed by Bedrock embeddings.

DynamoDB's native vector search (SearchVectors + VectorIndexes) runs locally: an
oblako proxy in front of DynamoDB Local captures the vector index and answers
SearchVectors as brute-force KNN, and the new API is grafted onto boto3 so an
unpatched client can call it. The embeddings come from oblako's local Bedrock
runtime (Ollama), so the whole embed -> store -> search loop is local.

Run:
    oblako up                     # DynamoDB Local
    ollama pull mxbai-embed-large # any local embedding model
    uv run examples/python/dynamodb/vector_search.py

Requires Docker (DynamoDB Local) and Ollama with an embedding model pulled.
"""

import time

from oblako.engines.bedrock.adapter import BedrockAdapter
from oblako.engines.bedrock.ollama_client import OllamaClient
from oblako.services import DynamoDBService

TABLE = "documents"


def _embedding_model() -> str:
    """Pick a pulled Ollama embedding model."""
    for model in OllamaClient().list_models():
        if "embed" in model["name"]:
            return f"ollama.{model['name']}"
    raise SystemExit("pull an embedding model first, e.g. `ollama pull mxbai-embed-large`")


def embed(adapter: BedrockAdapter, model: str, text: str) -> list[float]:
    """Return an embedding vector for text via the local Bedrock runtime."""
    import json

    result = adapter.invoke_model(model, json.dumps({"inputText": text}))
    return result["embedding"]


def main():
    ddb = DynamoDBService().get_vector_client()
    adapter = BedrockAdapter()
    model = _embedding_model()
    dims = len(embed(adapter, model, "probe"))

    ddb.create_table(
        TableName=TABLE,
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
        VectorIndexes=[
            {
                "IndexName": "by-embedding",
                "VectorAttribute": {"AttributeName": "embedding"},
                "Dimensions": dims,
                "DistanceFunction": "COSINE",
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    for _ in range(30):
        if ddb.describe_table(TableName=TABLE)["Table"]["TableStatus"] == "ACTIVE":
            break
        time.sleep(0.2)

    corpus = {
        "d1": "The cat sat on the warm windowsill in the afternoon sun.",
        "d2": "Quarterly revenue grew twelve percent on strong cloud sales.",
        "d3": "A kitten curled up by the sunny window and fell asleep.",
        "d4": "The rocket reached orbit nine minutes after liftoff.",
    }
    for doc_id, text in corpus.items():
        ddb.put_item(
            TableName=TABLE,
            Item={
                "id": {"S": doc_id},
                "text": {"S": text},
                "embedding": {"L": [{"N": str(v)} for v in embed(adapter, model, text)]},
            },
        )

    query = "a sleepy cat in the sunshine"
    resp = ddb.search_vectors(
        TableName=TABLE,
        IndexName="by-embedding",
        SearchVector=[{"N": str(v)} for v in embed(adapter, model, query)],
        TopK=2,
    )
    print(f"query: {query!r}\n")
    for result in resp["SearchResults"]:
        item = result["Item"]
        print(f"  {result['Score']:.4f}  {item['id']['S']}: {item['text']['S']}")


if __name__ == "__main__":
    main()
