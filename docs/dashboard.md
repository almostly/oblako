# Dashboard

`oblako dashboard` launches a web UI built with the **AWS Cloudscape Design
System**, the same components as the real AWS Console, at
<http://localhost:8000>. It's a read-and-act view over everything that's running.

It listens on `127.0.0.1`, so only your own machine can open it: it has no login,
and its Notebook page runs Python against your services. `--host` makes it listen
elsewhere, with a warning; do that only on a network you trust.

## Pages

- **Services**: status overview of all running services.
- **Notebook**: a Python editor with syntax highlighting that runs code against
  all your local services.
- **Bedrock**: chat playground powered by Ollama (or OpenRouter).
- **SageMaker**: training jobs, endpoints, Docker images, and cleanup.
- **S3**: bucket browser with object listing.
- **Athena**: a SQL editor. Trino reads Iceberg and S3 Tables through the catalog,
  and DuckDB-Wasm runs in your browser against Parquet files in S3.
- **Glue**: Data Catalog databases and tables (the same tables Athena sees), and
  PySpark jobs in the Glue 5 image.
- **Kinesis**: streams, with a record writer and reader.
- **DynamoDB**: table browser with an item viewer.
- **RDS / Aurora**: instances and clusters, with database creation.
- **Step Functions**: state machines, ASL JSON viewer, execution history, and a
  flow diagram.
- **CloudFormation**: stacks deployed to the local CloudFormation, with their
  resources, outputs, and events.
- **Redshift**: cluster list (management API), table list, and a SQL query
  editor with results.
- **Lambda**: functions and layers, with function creation and invocation.
- **EC2**: instances, each backed by a real container, with instance launch.
- **IAM**: users, roles and policies, with assume-role and an access simulator.
- **AppConfig**: applications, configuration profiles and deployments, with a
  feature-flag evaluator.

## Notebook (JupyterLab)

`oblako notebook` launches **JupyterLab** with the kernel pre-wired to oblako, so
you write the AWS code you normally would and it runs against your local
services, no `endpoint_url`, no config:

```bash
oblako up                      # start the services
pip install 'oblako[notebook]'
oblako notebook                # JupyterLab on http://localhost:8888
```

```python
import boto3
s3 = boto3.client("s3")        # transparently hits S3Proxy, no endpoint_url
s3.create_bucket(Bucket="from-notebook")
```

Under the hood the kernel gets `AWS_ENDPOINT_URL_*` for every service plus test
credentials and S3 path-style/checksum config. The always-on Docker services
work immediately; the in-process servers (CloudFormation, redshift-data,
rds-data, bedrock-runtime) start on first use.
