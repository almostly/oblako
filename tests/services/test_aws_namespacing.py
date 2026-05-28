"""Platform namespacing mirrors how AWS exposes these services.

MLflow on AWS lives under SageMaker (sagemaker:CreateMlflowTrackingServer), and
"S3 Tables" is the AWS-managed Iceberg catalog. oblako mirrors both.
"""

from oblako.services.platform import Oblako


def test_mlflow_is_a_sagemaker_resource():
    o = Oblako()
    assert o.sagemaker.mlflow is o.mlflow


def test_s3tables_is_iceberg():
    o = Oblako()
    assert o.s3tables is o.iceberg
