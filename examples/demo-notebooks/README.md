# Demo notebooks on oblako

Real AWS workflows running entirely against oblako — same boto3 / SDK code as on
AWS, only the endpoints change. Cases are grouped per folder so it's easy to find
a starting point for your use case.

| Case | What it shows | Folder |
|---|---|---|
| **Credit risk** (CatBoost scorecard) | BYOC container → SageMaker local training → endpoint → SageMaker Pipelines | [`credit-risk/`](./credit-risk) |
| **Fraud detection** (Linear Learner) | Real-time fraud — Kinesis transaction stream + built-in `LinearLearner` training/endpoint | [`fraud/`](./fraud) |
| **Feast feature store** | Feast on redshift-local (offline) + DynamoDB Local (online): apply → historical features → materialize → serve | [`feast/`](./feast) |

More cases are easy to add — drop a new folder here following the same shape.
Hugging Face is a natural next addition (BYOC pattern with a transformers image).

The notebooks in each case run unchanged against AWS — the only difference is
that `endpoint_url` / `AWS_ENDPOINT_URL_*` is set to point at oblako.
