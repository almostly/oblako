# SageMaker on oblako: credit scoring (CatBoost)

Three notebooks that run a SageMaker credit-scoring workflow **fully locally** against
oblako, adapted from [`aws-samples/credit-risk-modeling-on-aws`](https://github.com/deburky/credit-risk-modeling-on-aws):

| Notebook | What it does | Reference |
|---|---|---|
| `01_byoc_training.ipynb` | Build a **bring-your-own-container** (CatBoost), train it in SageMaker local mode, store the model in S3Proxy | `sagemaker_endpoints/ml_models` |
| `02_endpoint_deployment.ipynb` | Deploy the model to a **local endpoint** and score loan applications (PD + SHAP scorecard score) | `sagemaker_endpoints/.../sagemaker_endpoint.py` + `inference.py` |
| `03_sagemaker_pipeline.ipynb` | A **SageMaker Pipeline** (process → train) run locally with `LocalPipelineSession` | `batch_scoring/.../sagemaker_pipeline.py` |

## The model

A **CatBoost** classifier over 7 numeric + 5 categorical application features
(Application/Bureau scores, loan-to-income, residential status, …), predicting
probability of default. At serving time the score comes from CatBoost's **native
SHAP** values: `score = offset + factor·(-log_odds)` (higher score = better credit),
the same scorecard the reference uses.

## The container: `container/`

One bring-your-own-container that both **trains** (`<image> train`) and **serves**
(`<image> serve`): `src/train.py` fits CatBoost + writes the scorecard metadata;
`src/serve.py` answers `GET /ping` + `POST /invocations` with PD + score.

## Run

```bash
pip install 'oblako[sagemaker,notebook]'
oblako up s3             # S3Proxy on :9000 (model store + pipeline artifacts)
# Docker running

oblako notebook          # opens JupyterLab; these notebooks are in the workspace
# ...or run a notebook headless:
jupyter nbconvert --to notebook --execute 01_byoc_training.ipynb
```

The only swaps from AWS are local: S3 → oblako's **S3Proxy**,
`Mode.LOCAL_CONTAINER` / `instance_type="local"` instead of `ml.*`, and
`use_local_stubs()` in place of IAM and ECR (the image is built locally).
Everything else is the real SageMaker SDK v3: `ModelTrainer` for training, the
`LocalSession` endpoint APIs for serving, and `sagemaker.mlops` `Pipeline`,
`ProcessingStep` and `TrainingStep` for the pipeline.
