"""Auto-mark integration / kubernetes tests so CI can select by category.

CI is then:
  unit:        pytest -m "not integration and not kubernetes"
  integration: pytest -m integration
  kubernetes:  pytest -m kubernetes

Adding a new integration test = drop it into one of the integration dirs / files
listed below; no need to touch ci.yml.
"""

from __future__ import annotations

import pytest

# Whole directories whose tests all require running oblako services (Docker).
_INTEGRATION_DIRS = (
    "tests/s3/",
    "tests/rds/",
    "tests/opensearch/",
    "tests/sagemaker/",
    "tests/awslambda/",
)

# Individual integration tests inside mixed directories (the rest of the dir is
# unit). test_cloudformation.py keeps its own skipif guards (mixed at function
# level) and stays out of this list.
_INTEGRATION_FILES = {
    "tests/bedrock/test_bedrock_control.py",
    "tests/bedrock/test_bedrock_runtime.py",
    "tests/bedrock/test_ollama.py",
    "tests/bedrock/test_bedrock_openrouter_live.py",
    "tests/redshift/test_redshift.py",
    "tests/redshift/test_redshift_catalog.py",
    "tests/redshift/test_redshift_data.py",
    "tests/redshift/test_redshift_copy_unload.py",
    "tests/redshift/test_redshift_super.py",
    "tests/redshift/test_redshift_listagg.py",
    "tests/redshift/test_redshift_functions.py",
    "tests/redshift/test_redshift_ml.py",
    "tests/redshift/test_redshift_proxy.py",
    "tests/redshift/test_redshift_sqlalchemy.py",
    "tests/redshift/test_redshift_cluster.py",
    "tests/stepfunctions/test_stepfunctions.py",
    "tests/services/test_ec2.py",
}

_KUBERNETES_FILES = {
    "tests/services/test_k8s_backend.py",
}


def pytest_collection_modifyitems(config, items):
    """Auto-apply integration / kubernetes markers based on the test's path."""
    root = config.rootpath
    for item in items:
        rel = str(item.path.relative_to(root))
        if rel.startswith(_INTEGRATION_DIRS) or rel in _INTEGRATION_FILES:
            item.add_marker(pytest.mark.integration)
        if rel in _KUBERNETES_FILES:
            item.add_marker(pytest.mark.kubernetes)
