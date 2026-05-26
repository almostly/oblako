"""OpenSearch service: vector search for Knowledge Bases / RAG."""

import httpx

from .base import Service, PortMapping


class OpenSearchService(Service):
    def __init__(self, host_port: int = 9200):
        super().__init__(
            name="opensearch",
            image="opensearchproject/opensearch:2",
            ports=[PortMapping(container_port=9200, host_port=host_port)],
            environment={
                "discovery.type": "single-node",
                "DISABLE_SECURITY_PLUGIN": "true",
                "OPENSEARCH_INITIAL_ADMIN_PASSWORD": "admin",
            },
            volumes={"oblako-ml-opensearch": {"bind": "/usr/share/opensearch/data", "mode": "rw"}},
        )
        self.host_port = host_port

    @property
    def url(self) -> str:
        return f"http://localhost:{self.host_port}"

    def _health_check(self) -> bool:
        try:
            resp = httpx.get(f"{self.url}/_cluster/health", timeout=3.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            # a starting OpenSearch may accept then reset the connection
            # (httpx.ReadError), not just refuse it — any transport error = not ready
            return False

    def create_knn_index(self, index_name: str, dimension: int = 1536) -> dict:
        """Create a k-NN vector index (for RAG / Knowledge Bases)."""
        resp = httpx.put(
            f"{self.url}/{index_name}",
            json={
                "settings": {"index": {"knn": True}},
                "mappings": {
                    "properties": {
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": dimension,
                            "method": {"name": "hnsw", "engine": "faiss"},
                        },
                        "text": {"type": "text"},
                        "metadata": {"type": "object"},
                    }
                },
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()
