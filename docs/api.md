# Python API

The `oblako.services` package. Each service is the local counterpart of an AWS
service, reached through its normal `boto3` client (`get_client()`) or native
driver (`connect()`). `Oblako` is the platform handle that owns them all.

## Platform

```{eval-rst}
.. autoclass:: oblako.services.platform.Oblako
   :members:
```

## AI / ML

```{eval-rst}
.. autoclass:: oblako.services.bedrock.BedrockService
   :members:
.. autoclass:: oblako.services.sagemaker.SageMakerService
   :members:
.. autoclass:: oblako.services.mlflow.MlflowService
   :members:
```

## Storage & databases

```{eval-rst}
.. autoclass:: oblako.services.s3proxy.S3ProxyService
   :members:
.. autoclass:: oblako.services.dynamodb.DynamoDBService
   :members:
.. autoclass:: oblako.services.kinesis.KinesisService
   :members:
.. autoclass:: oblako.services.redshift.RedshiftService
   :members:
.. autoclass:: oblako.services.rds.RdsService
   :members:
.. autoclass:: oblako.services.iceberg.IcebergCatalogService
   :members:
```

## Analytics

```{eval-rst}
.. autoclass:: oblako.services.trino.TrinoService
   :members:
.. autoclass:: oblako.services.glue.GlueService
   :members:
.. autoclass:: oblako.services.glue_catalog.GlueCatalogService
   :members:
```

## Orchestration, compute & management

```{eval-rst}
.. autoclass:: oblako.services.stepfunctions.StepFunctionsService
   :members:
.. autoclass:: oblako.services.awslambda.LambdaService
   :members:
.. autoclass:: oblako.services.opensearch.OpenSearchService
   :members:
.. autoclass:: oblako.services.cloudformation.CloudFormationService
   :members:
.. autoclass:: oblako.services.iam.IamService
   :members:
.. autoclass:: oblako.services.ec2.Ec2Service
   :members:
.. autoclass:: oblako.services.appconfig.AppConfigService
   :members:
```

## Container backends

The lifecycle layer behind every service: Docker, Apple `container`, or
Kubernetes (selected with `OBLAKO_CONTAINER_BACKEND`).

```{eval-rst}
.. automodule:: oblako.services.backends
   :members: ContainerBackend, DockerBackend, AppleContainerBackend, KubernetesBackend
```
