"""Live integration test for the Kubernetes container backend.

Skips unless a cluster is reachable (kubectl + a current context). In CI a kind
cluster provides one; locally `minikube start` does. Runs a real service
(DynamoDB) as a Deployment+Service and reaches it with boto3 via port-forward.
"""

import shutil
import subprocess
import time

import pytest


def _cluster_available() -> bool:
    if shutil.which("kubectl") is None:
        return False
    return (
        subprocess.run(["kubectl", "cluster-info"], capture_output=True).returncode == 0
    )


pytestmark = pytest.mark.skipif(
    not _cluster_available(), reason="no Kubernetes cluster reachable"
)


def test_dynamodb_runs_on_kubernetes(monkeypatch):
    monkeypatch.setenv("OBLAKO_CONTAINER_BACKEND", "kubernetes")
    from oblako.services.dynamodb import DynamoDBService

    svc = DynamoDBService(host_port=8056)
    assert svc.backend.name == "kubernetes"

    svc.stop()  # clean any leftover from a previous run
    try:
        svc.start()
        assert svc.wait_ready(timeout=180), "dynamodb did not become ready on k8s"
        client = svc.get_client()
        client.create_table(
            TableName="ci-k8s",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        time.sleep(2)
        assert "ci-k8s" in client.list_tables()["TableNames"]
    finally:
        svc.stop()
