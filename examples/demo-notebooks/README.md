# Demo notebooks on oblako

Real AWS workflows running entirely against oblako: the same boto3 / SDK code as on
AWS, only the endpoints change. Cases are grouped per folder so it's easy to find
a starting point for your use case.

| Case | What it shows | Folder |
|---|---|---|
| **Credit scoring** (CatBoost scorecard) | BYOC container → SageMaker local training → endpoint → SageMaker Pipelines | [`credit-scoring/`](./credit-scoring) |
| **Fraud detection** (Linear Learner) | Real-time fraud: Kinesis transaction stream + built-in `LinearLearner` training/endpoint | [`fraud/`](./fraud) |
| **Feast feature store** | Feast on redshift-local (offline) + DynamoDB Local (online): apply → historical features → materialize → serve | [`feast/`](./feast) |

More cases are easy to add: drop a new folder here following the same shape.
Hugging Face is a natural next addition (BYOC pattern with a transformers image).

The AWS calls are the ones you would make on AWS. The notebooks take their clients
from oblako (`S3ProxyService().get_client()` and friends) and train with the
SageMaker SDK's local mode; on AWS those become `boto3.client(...)` and a real
training job.
