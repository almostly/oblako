"""Oblako: start/stop all oblako services together."""

from __future__ import annotations

from .appconfig import AppConfigService
from .bedrock import BedrockService
from .caddy import CaddyService
from .cloudformation import CloudFormationService
from .dynamodb import DynamoDBService
from .ec2 import Ec2Service
from .glue import GlueService
from .glue_catalog import GlueCatalogService
from .iam import IamService
from .iceberg import IcebergCatalogService
from .kinesis import KinesisService
from .awslambda import LambdaService
from .mlflow import MlflowService
from .moto import MotoService
from .opensearch import OpenSearchService
from .rds import RdsService
from .redshift import PgRedshiftService, RedshiftService
from .s3proxy import S3ProxyService
from .sagemaker import SageMakerService
from .stepfunctions import StepFunctionsService
from .trino import TrinoService


class Oblako:
    """Manage the full oblako service stack."""

    def __init__(self):
        """Initialize all oblako services."""
        self.bedrock = BedrockService()
        self.ollama = self.bedrock  # backwards-compatible alias (engine is Ollama)
        self.opensearch = OpenSearchService()
        # Redshift = raw pgredshift engine (5438) fronted by a wire proxy that is
        # the Redshift endpoint (5439). The proxy lets redshift-connector clients
        # (e.g. dbt-redshift) connect; psycopg2 passes through untouched.
        # See services/redshift.py.
        self.pgredshift = PgRedshiftService()
        self.redshift = RedshiftService()
        self.rds = RdsService()
        self.aurora = self.rds  # Aurora shares the rds control plane + engine
        self.moto = MotoService()
        self.iam = IamService(
            moto=self.moto
        )  # IAM/STS control plane + policy evaluator
        # Lambda: moto's control plane + real Docker exec (socket mounted into moto).
        # Canonical attribute is `awslambda` (matches moto's own module name);
        # `lambda_` stays as a boto3-style alias since `lambda` is a keyword.
        self.awslambda = LambdaService(moto=self.moto)
        self.lambda_ = self.awslambda
        # EC2: moto's control plane (instances, VPCs, security groups — describe
        # fidelity). Shares the moto endpoint, like IAM/Lambda.
        self.ec2 = Ec2Service(moto=self.moto)
        self.s3 = S3ProxyService()
        self.dynamodb = DynamoDBService(host_port=8001)
        self.kinesis = KinesisService()
        # Iceberg REST catalog backed by S3Proxy. On AWS this is "S3 Tables"
        # (boto3.client("s3tables")) — same shape, same Iceberg under the hood.
        self.iceberg = IcebergCatalogService()
        self.s3tables = self.iceberg  # AWS-faithful alias: S3 Tables == Iceberg-on-S3
        # Glue 5 PySpark runner (per-job container; ~5 GB image, pulled on first job).
        self.glue = GlueService()
        # Glue Data Catalog (boto3 `glue` client) bridged to the Iceberg REST catalog.
        self.glue_catalog = GlueCatalogService()
        # Trino — Athena equivalent. SQL over the Iceberg catalog / S3 Tables.
        self.trino = TrinoService()
        self.athena = self.trino  # AWS-faithful alias: Athena is Trino under the hood
        self.stepfunctions = StepFunctionsService()
        # MLflow on AWS is a SageMaker resource (sagemaker:CreateMlflowTrackingServer),
        # so it lives under the SageMaker namespace: oblako.sagemaker.mlflow is the
        # canonical, AWS-faithful handle. oblako.mlflow stays as a convenience alias
        # (same object), matching oblako's alias convention (athena, ollama, …).
        self.sagemaker = SageMakerService()
        self.sagemaker.mlflow = MlflowService()
        self.mlflow = self.sagemaker.mlflow
        # Caddy reverse proxy fronts services with AWS-style vanity hostnames
        # (mlflow.oblako.aws -> MLflow). Also lazy — started with /api/mlflow/launch.
        self.caddy = CaddyService()
        # In-process orchestration server (no container): provisions stacks into
        # the engines above. Started lazily on first get_client().
        self.cloudformation = CloudFormationService()
        # AppConfig (control + data plane) + the rule-evaluation agent — also an
        # in-process server, started lazily on first get_client().
        self.appconfig = AppConfigService()

    @property
    def _docker_services(self):
        """All managed containers (started/stopped together)."""
        return [
            self.bedrock,
            self.opensearch,
            self.pgredshift,  # engine first — the proxy in front of it depends on it
            self.redshift,
            self.rds,
            self.moto,
            self.s3,
            self.dynamodb,
            self.stepfunctions,
        ]

    @property
    def _infra_services(self):
        """Internal infra containers: started with `up`, hidden from `status`.

        moto backs the Redshift/RDS/Aurora control planes; it is plumbing, not
        a peer service like S3 or DynamoDB. pgredshift is the raw engine behind
        the Redshift endpoint (`redshift`), so it is hidden the same way.
        """
        return [self.moto, self.pgredshift]

    def up(self) -> None:
        """Start all Docker-managed services."""
        for svc in self._docker_services:
            svc.start()

    def down(self) -> None:
        """Stop all Docker-managed services."""
        for svc in self._docker_services:
            svc.stop()

    def status(self) -> dict[str, str]:
        """Return status of user-facing services (infra like moto is hidden)."""
        result = {}
        for svc in self._docker_services:
            if svc in self._infra_services:
                continue
            result[svc.name] = svc.status().value
        # SageMaker is Docker-native, report container count
        sm_containers = self.sagemaker.list_training_containers()
        result["sagemaker"] = (
            f"{len(sm_containers)} containers" if sm_containers else "idle"
        )
        return result

    def wait_ready(self, timeout: float = 60.0) -> dict[str, bool]:
        """Wait for all services to be ready."""
        return {
            svc.name: svc.wait_ready(timeout=timeout) for svc in self._docker_services
        }
