# Real-time credit card fraud detection (Linear Learner + Kinesis)

Adapted from the AWS [credit-card-fraud reference](https://github.com/aws-samples/aws-fraud-detector-samples).
Two notebooks:

| | What it does |
|---|---|
| `01_kinesis_transaction_stream.ipynb` | Reads transactions from a CSV and `put_record`s them onto a Kinesis stream (`TransactionsStream`) — the "simulator" half of real-time fraud detection. |
| `02_linear_learner_training.ipynb` | Builds a bring-your-own-container **linear SVM** (hinge loss + balanced classes — the same model as the managed `LinearLearner`), trains it in SageMaker **local mode** (`instance_type="local"`), evaluates precision/recall on held-out transactions, and stores the model in oblako's S3Proxy. No AWS account. |

## Running against oblako

Both notebooks use real boto3 / SageMaker SDK calls. To point them at oblako:

**Kinesis** (notebook 01): set `AWS_ENDPOINT_URL_KINESIS` before constructing the
client, or pass `endpoint_url` explicitly:

```python
import os, boto3
os.environ["AWS_ENDPOINT_URL_KINESIS"] = "http://localhost:4567"  # KinesisService
kinesis = boto3.client("kinesis", aws_access_key_id="test",
                       aws_secret_access_key="test", region_name="us-east-1")
kinesis.create_stream(StreamName="TransactionsStream", ShardCount=1)
```

**SageMaker training/endpoint** (notebook 02): the AWS notebook targets real
SageMaker (`ml.m5.large` etc.). For local mode, follow the same `LocalSession()`
+ `instance_type="local"` pattern from
[`../credit-risk/01_byoc_training.ipynb`](../credit-risk/01_byoc_training.ipynb):

```python
from sagemaker.local import LocalSession
session = LocalSession(); session.config = {"local": {"local_code": True}}
linear = LinearLearner(..., instance_type="local", sagemaker_session=session)
```

S3 paths in either notebook should point at S3Proxy
(`AWS_ENDPOINT_URL_S3=http://localhost:9000` and path-style addressing — see
`oblako/notebook.py`'s `make_env` for the full env recipe).
