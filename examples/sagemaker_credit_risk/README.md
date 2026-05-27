# SageMaker on oblako — credit-risk modeling, end to end

A complete SageMaker workflow running **fully locally** against oblako: train a
credit-scoring model, deploy it to a real-time endpoint, and orchestrate it with
a SageMaker Pipeline — the same code you'd run on AWS, no cloud, no ECR.

The model is a credit **scorecard**: a logistic-regression probability of default
scaled into a score with the standard PDO formula
(`score = offset + factor·ln(odds)`), then turned into an APPROVE/DECLINE decision
against a cutoff. (Patterns adapted from `aws-samples/credit-risk-modeling-on-aws`;
the local training/serving/pipeline execution is oblako's.)

## The bring-your-own-container

`container/` is one image that both **trains** and **serves** (the classic
SageMaker BYOC contract — SageMaker runs it as `<image> train` and `<image> serve`):
- `src/train.py` — fits the PD model, bakes in the scorecard scaling, writes the
  model + metadata to `/opt/ml/model`.
- `src/serve.py` — loads the model and answers `GET /ping` + `POST /invocations`
  with PD, score, and decision.

## Prerequisites

```bash
pip install 'oblako[sagemaker]'
oblako up s3proxy        # S3Proxy on :9000 (model store + pipeline artifacts)
# Docker running
```

## 1. Train → store in oblako's S3

```bash
python examples/sagemaker_credit_risk/01_train.py
```
Builds the container, trains it in **SageMaker local mode** (`instance_type="local"`
— a real Docker training job), and uploads `model.tar.gz` to S3Proxy:
```
trained on 500 rows, 5 features (default rate 0.32); factor=28.9 offset=501.9
Model stored in oblako S3: s3://credit-risk-models/credit-risk/model.tar.gz
```

## 2. Deploy a local endpoint → score applications

```bash
python examples/sagemaker_credit_risk/02_endpoint.py
```
`estimator.deploy(instance_type="local")` spins up the serving container; the
Predictor scores applications in real time:
```
Credit decisions:
  income= 150000 util=0.05 delinq=0  ->  PD=0.018 score=617 APPROVE
  income=  28000 util=0.95 delinq=3  ->  PD=0.990 score=369 DECLINE
```

## 3. SageMaker Pipeline (process → train), run locally

```bash
python examples/sagemaker_credit_risk/03_pipeline.py
```
A `LocalPipelineSession` pipeline: a `ProcessingStep` (`prep.py`) generates the
training data and a `TrainingStep` fits the model on it — wired together exactly
as on AWS, with inter-step artifacts passing through S3Proxy:
```
Pipeline execution: Succeeded
  GenerateData       Succeeded
  TrainCreditModel   Succeeded
```

## How it maps to AWS
- **Training** uses real SageMaker local mode (Docker), not a mock — the same
  `Estimator` / `fit` you'd run in the cloud.
- **The endpoint** is a real serving container honoring SageMaker's `/ping` +
  `/invocations` contract.
- **The pipeline** uses the real `sagemaker.workflow` API (`Pipeline`,
  `ProcessingStep`, `TrainingStep`); `LocalPipelineSession` executes it locally.
- The only swaps are local: S3 → oblako's **S3Proxy**, and `instance_type="local"`
  instead of `ml.*` instances. No ECR (the image is built locally), no IAM.
