# Real-time credit card fraud detection (Linear Learner + Kinesis)

Adapted from the AWS [credit-card-fraud reference](https://github.com/aws-samples/aws-fraud-detector-samples).
Two notebooks:

| | What it does |
|---|---|
| `01_kinesis_transaction_stream.ipynb` | Reads transactions from a CSV and `put_record`s them onto a Kinesis stream (`TransactionsStream`) — the "simulator" half of real-time fraud detection. |
| `02_linear_learner_training.ipynb` | Builds a bring-your-own-container **linear SVM** (hinge loss + balanced classes, the same model as the managed `LinearLearner`), trains it with the SDK v3 `ModelTrainer` in **local mode** (`Mode.LOCAL_CONTAINER`), evaluates precision/recall on held-out transactions, and stores the model in oblako's S3Proxy. No AWS account. |

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
SageMaker (`ml.m5.large` etc.). Locally, the notebook trains the
same hinge-loss linear model in a bring-your-own-container with the SDK v3
`ModelTrainer` in local mode:

```python
from sagemaker.train import ModelTrainer
from sagemaker.train.configs import Compute, InputData
from sagemaker.train.model_trainer import Mode

from oblako.engines.sagemaker import use_local_stubs

use_local_stubs()  # no AWS account: skip the SDK's IAM role check
trainer = ModelTrainer(
    training_image="oblako-fraud:latest",
    training_mode=Mode.LOCAL_CONTAINER,
    compute=Compute(instance_type="local", instance_count=1),
    role="arn:aws:iam::000000000000:role/dummy",
    local_container_root=str(work / "job"),  # under /Users, not the temp dir
)
trainer.train(input_data_config=[InputData(channel_name="train", data_source=str(work / "train"))])
```

S3 paths in either notebook should point at S3Proxy
(`AWS_ENDPOINT_URL_S3=http://localhost:9000` and path-style addressing — see
`oblako/notebook.py`'s `make_env` for the full env recipe).
