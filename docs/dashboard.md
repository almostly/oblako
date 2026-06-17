# Dashboard

`oblako dashboard` launches a web UI built with the **AWS Cloudscape Design
System**, the same components as the real AWS Console, at
<http://localhost:8000>. It's a read-and-act view over everything that's running.

## Pages

- **Services**: status overview of all running services.
- **Notebook**: a Python editor with syntax highlighting that runs code against
  all your local services.
- **Bedrock**: chat playground powered by Ollama (or OpenRouter).
- **SageMaker**: training jobs, endpoints, Docker images, and cleanup.
- **S3**: bucket browser with object listing.
- **DynamoDB**: table browser with an item viewer.
- **Step Functions**: state machines, ASL JSON viewer, execution history, and a
  flow diagram.
- **CloudFormation**: stacks deployed to the local CloudFormation, with their
  resources, outputs, and events.
- **Redshift**: cluster list (management API), table list, and a SQL query
  editor with results.

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
