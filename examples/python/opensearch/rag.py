"""OpenSearch vector search for RAG / Knowledge Bases.

Mirrors the pattern from deploy-amazon-bedrock-agent: index documents,
then retrieve relevant chunks using k-NN vector search.

Prerequisites:
    make up
    (Optional: ollama pull nomic-embed-text for real embeddings)
"""

import hashlib
import random

from opensearchpy import OpenSearch

from oblako.services import OpenSearchService

os_svc = OpenSearchService()
client = OpenSearch(hosts=[{"host": "localhost", "port": 9200}], use_ssl=False)

INDEX = "credit-risk-knowledge-base"

# Create k-NN index (same pattern as Bedrock Knowledge Base)
if client.indices.exists(index=INDEX):
    client.indices.delete(index=INDEX)

client.indices.create(
    index=INDEX,
    body={
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "embedding": {
                    "type": "knn_vector",
                    "dimension": 64,
                    "method": {"name": "hnsw", "engine": "faiss"},
                },
                "text": {"type": "text"},
                "source": {"type": "keyword"},
                "chunk_id": {"type": "integer"},
            }
        },
    },
)
print(f"Created index: {INDEX}")


# Simulate document chunks (in production, these come from PDF parsing + embeddings)
def fake_embedding(text: str, dim: int = 64) -> list[float]:
    """Deterministic fake embedding based on text hash."""
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32)
    rng = random.Random(seed)
    return [rng.gauss(0, 1) for _ in range(dim)]


documents = [
    {
        "text": "Credit risk is the possibility of a loss resulting from a borrower's failure to repay a loan.",
        "source": "policy/credit-risk-overview.pdf",
    },
    {
        "text": "The probability of default (PD) measures the likelihood that a borrower will default within a given time period.",
        "source": "policy/credit-risk-overview.pdf",
    },
    {
        "text": "Loss Given Default (LGD) estimates the portion of exposure that will be lost if a default occurs.",
        "source": "policy/credit-risk-overview.pdf",
    },
    {
        "text": "A credit scorecard assigns points to borrower characteristics to produce a numerical credit score.",
        "source": "policy/scorecard-methodology.pdf",
    },
    {
        "text": "Weight of Evidence (WOE) transformation converts categorical variables into continuous values based on event rates.",
        "source": "policy/scorecard-methodology.pdf",
    },
    {
        "text": "The Gini coefficient measures the discriminatory power of a credit scoring model, ranging from 0 to 1.",
        "source": "policy/scorecard-methodology.pdf",
    },
    {
        "text": "Basel III requires banks to hold capital reserves proportional to the credit risk of their loan portfolios.",
        "source": "policy/regulatory-requirements.pdf",
    },
    {
        "text": "IFRS 9 mandates expected credit loss provisioning based on forward-looking macroeconomic scenarios.",
        "source": "policy/regulatory-requirements.pdf",
    },
]

for i, doc in enumerate(documents):
    client.index(
        index=INDEX,
        id=str(i),
        body={
            "text": doc["text"],
            "source": doc["source"],
            "chunk_id": i,
            "embedding": fake_embedding(doc["text"]),
        },
    )
print(f"Indexed {len(documents)} document chunks")

client.indices.refresh(index=INDEX)

# Query: find relevant chunks for a question
query = "How do you measure the quality of a credit scoring model?"
query_embedding = fake_embedding(query)

results = client.search(
    index=INDEX,
    body={
        "size": 3,
        "query": {
            "knn": {
                "embedding": {
                    "vector": query_embedding,
                    "k": 3,
                }
            }
        },
    },
)

print(f"\nQuery: {query}")
print(f"Top {len(results['hits']['hits'])} results:")
for hit in results["hits"]["hits"]:
    src = hit["_source"]
    print(f"  [{hit['_score']:.3f}] {src['text'][:80]}...")
    print(f"         source: {src['source']}")

# Text search (BM25) works too
print("\nText search for 'Basel':")
results = client.search(index=INDEX, body={"query": {"match": {"text": "Basel"}}})
for hit in results["hits"]["hits"]:
    print(f"  {hit['_source']['text'][:80]}...")

client.indices.delete(index=INDEX)
print(f"\nCleaned up index: {INDEX}")
