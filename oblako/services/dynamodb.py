"""DynamoDB Local service: official AWS Docker image."""

from oblako import ports
from .base import Service, PortMapping
from .boto import BotoService, resource


@BotoService("dynamodb")
class DynamoDBService(Service):
    """DynamoDB Local service backed by the official Amazon Docker image."""

    def __init__(self, host_port: int = ports.DYNAMODB):
        """Initialize the DynamoDB Local service on the given host port."""
        super().__init__(
            name="dynamodb",
            image="amazon/dynamodb-local:latest",
            ports=[PortMapping(container_port=8000, host_port=host_port)],
            # Persist to the mounted volume: dynamodb-local only writes to disk
            # with -sharedDb -dbPath (else it runs per-credentials, in-memory-ish).
            command="-jar DynamoDBLocal.jar -sharedDb -dbPath ./data",
            working_dir="/home/dynamodblocal",
            volumes={
                "oblako-dynamodb-data": {"bind": "/home/dynamodblocal/data", "mode": "rw"}
            },
            # The named volume is root-owned; run as root so the non-root default
            # user can write shared-local-instance.db (else: "unable to open database file").
            container_user="root",
        )
        self.host_port = host_port

    @property
    def endpoint_url(self) -> str:
        """Return the DynamoDB Local endpoint URL."""
        return f"http://localhost:{self.host_port}"

    def get_resource(self):
        """Return a boto3 DynamoDB resource for higher-level API."""
        return resource("dynamodb", self.endpoint_url)

    def _health_check(self) -> bool:
        try:
            client = self.get_client()
            client.list_tables()
            return True
        except Exception:
            return False
