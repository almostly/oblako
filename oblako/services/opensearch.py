"""OpenSearch service: vector search for Knowledge Bases / RAG."""

from oblako import ports
import httpx

from .base import Service, PortMapping


class OpenSearchService(Service):
    """OpenSearch service for vector search and RAG Knowledge Bases."""

    def __init__(self, host_port: int = ports.OPENSEARCH):
        """Initialize the OpenSearch service on the given host port."""
        super().__init__(
            name="opensearch",
            image="opensearchproject/opensearch:2",
            ports=[PortMapping(container_port=9200, host_port=host_port)],
            environment={
                "discovery.type": "single-node",
                "DISABLE_SECURITY_PLUGIN": "true",
                "OPENSEARCH_INITIAL_ADMIN_PASSWORD": "admin",
                # Local simulator: don't let disk-usage watermarks flip indices to
                # read-only (a full CI runner otherwise triggers
                # index_create_block_exception). No allocation decisions to gate on
                # a single node anyway.
                "cluster.routing.allocation.disk.threshold_enabled": "false",
            },
            volumes={
                "oblako-opensearch-data": {
                    "bind": "/usr/share/opensearch/data",
                    "mode": "rw",
                }
            },
        )
        self.host_port = host_port

    @property
    def url(self) -> str:
        """Return the OpenSearch base URL."""
        return f"http://localhost:{self.host_port}"

    def _health_check(self) -> bool:
        """Return True if OpenSearch reports its cluster health."""
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
