"""ML-focused Step Functions templates, runnable locally via SFN Local mock mode.

Adapted from aws-samples/credit-risk-modeling-on-aws. The reference definitions use
the JSONata query language and variables, which the `amazon/aws-stepfunctions-local`
image predates — so these are the equivalent classic JSONPath ASL. Each template
ships a mock test case: SFN Local returns the canned responses below for the ML Task
states, so an execution runs the real flow (Choice/Retry/threading) end to end without
a SageMaker control plane or a Lambda runtime.
"""

from __future__ import annotations

DUMMY_ROLE = "arn:aws:iam::123456789012:role/oblako-sfn"
S3 = "s3://credit-risk-models"
IMAGE = "credit-risk-xgboost:latest"
SM_ROLE = "arn:aws:iam::123456789012:role/oblako-sagemaker"

# A template is: name (= the created state-machine name, must match the mock config),
# comment, definition (classic JSONPath ASL), a sample execution input, a single mock
# test case, and the canned task outputs that case returns (keyed by short name).
TEMPLATES: dict[str, dict] = {}


def _retry() -> list:
    return [
        {
            "ErrorEquals": [
                "Lambda.ServiceException",
                "Lambda.TooManyRequestsException",
                "Lambda.SdkClientException",
            ],
            "IntervalSeconds": 1,
            "MaxAttempts": 3,
            "BackoffRate": 2,
        }
    ]


def _processing(next_state: str) -> dict:
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::sagemaker:createProcessingJob.sync",
        "Parameters": {
            "ProcessingJobName.$": "$$.Execution.Name",
            "RoleArn": SM_ROLE,
            "AppSpecification": {
                "ImageUri": "python:3.11-slim",
                "ContainerEntrypoint": [
                    "python3",
                    "/opt/ml/processing/input/code/transform.py",
                ],
            },
            "ProcessingResources": {
                "ClusterConfig": {
                    "InstanceCount": 1,
                    "InstanceType": "local",
                    "VolumeSizeInGB": 10,
                },
            },
            "ProcessingInputs": [
                {
                    "InputName": "input-1",
                    "S3Input": {
                        "S3Uri": f"{S3}/input/raw.csv",
                        "LocalPath": "/opt/ml/processing/input",
                        "S3DataType": "S3Prefix",
                        "S3InputMode": "File",
                    },
                }
            ],
            "ProcessingOutputConfig": {
                "Outputs": [
                    {
                        "OutputName": "train_data",
                        "S3Output": {
                            "S3Uri": f"{S3}/train",
                            "LocalPath": "/opt/ml/processing/output/train",
                            "S3UploadMode": "EndOfJob",
                        },
                    }
                ]
            },
            "StoppingCondition": {"MaxRuntimeInSeconds": 300},
        },
        "Next": next_state,
    }


def _training(*, end: bool = False, next_state: str | None = None) -> dict:
    state = {
        "Type": "Task",
        "Resource": "arn:aws:states:::sagemaker:createTrainingJob.sync",
        "Parameters": {
            "TrainingJobName.$": "$$.Execution.Name",
            "RoleArn": SM_ROLE,
            "AlgorithmSpecification": {
                "TrainingImage": IMAGE,
                "TrainingInputMode": "File",
            },
            "OutputDataConfig": {"S3OutputPath": f"{S3}/models"},
            "ResourceConfig": {
                "InstanceCount": 1,
                "InstanceType": "local",
                "VolumeSizeInGB": 30,
            },
            "StoppingCondition": {"MaxRuntimeInSeconds": 86400},
            "InputDataConfig": [
                {
                    "ChannelName": "train",
                    "ContentType": "text/csv",
                    "DataSource": {
                        "S3DataSource": {
                            "S3DataType": "S3Prefix",
                            "S3DataDistributionType": "ShardedByS3Key",
                            "S3Uri": f"{S3}/train",
                        }
                    },
                }
            ],
            "HyperParameters": {
                "objective": "binary:logistic",
                "eval_metric": "auc",
                "num_round": "50",
            },
        },
    }
    if end:
        state["End"] = True
    else:
        state["Next"] = next_state
    return state


def _save_model(next_state: str) -> dict:
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::sagemaker:createModel",
        "Parameters": {
            "ModelName.$": "$.TrainingJobName",
            "ExecutionRoleArn": SM_ROLE,
            "PrimaryContainer": {
                "Image": IMAGE,
                "ModelDataUrl.$": "$.ModelArtifacts.S3ModelArtifacts",
            },
        },
        "Next": next_state,
    }


def _batch_transform() -> dict:
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::sagemaker:createTransformJob.sync",
        "Parameters": {
            "TransformJobName.$": "$$.Execution.Name",
            "ModelName.$": "$.ModelName",
            "TransformInput": {
                "ContentType": "text/csv",
                "CompressionType": "None",
                "DataSource": {
                    "S3DataSource": {
                        "S3DataType": "S3Prefix",
                        "S3Uri": f"{S3}/score/applicants.csv",
                    }
                },
            },
            "TransformOutput": {"S3OutputPath": f"{S3}/scores"},
            "TransformResources": {"InstanceCount": 1, "InstanceType": "local"},
        },
        "End": True,
    }


def _generate(next_state: str) -> dict:
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::lambda:invoke",
        "Parameters": {"FunctionName": "credit-generate-dataset"},
        "OutputPath": "$.Payload",
        "Retry": _retry(),
        "Next": next_state,
    }


# Canned task outputs shared by the SageMaker templates.
_R_DATA = {"Payload": {"trainUri": f"{S3}/train/", "rows": 600}}
_R_PROCESSED = {"ProcessingJobStatus": "Completed"}
_R_TRAINED = {
    "TrainingJobStatus": "Completed",
    "TrainingJobName": "credit-xgb",
    "ModelArtifacts": {
        "S3ModelArtifacts": f"{S3}/models/credit-xgb/output/model.tar.gz"
    },
}
_R_MODEL = {
    "ModelArn": "arn:aws:sagemaker:us-east-1:123456789012:model/credit-xgb",
    "ModelName": "credit-xgb",
}
_R_TRANSFORM = {
    "TransformJobStatus": "Completed",
    "TransformOutput": {"S3OutputPath": f"{S3}/scores"},
}


TEMPLATES["preprocess-train"] = {
    "name": "credit-preprocess-train",
    "runnable": True,
    "comment": "Generate data (Lambda) -> standardize features (Processing) -> train XGBoost.",
    "input": {},
    "definition": {
        "Comment": "Preprocess then train a credit-risk model.",
        "StartAt": "Generate dataset",
        "States": {
            "Generate dataset": _generate("Standardize features"),
            "Standardize features": _processing("Train model (XGBoost)"),
            "Train model (XGBoost)": _training(end=True),
        },
    },
    "testCase": "happy-path",
    "mock": {
        "Generate dataset": "DataReady",
        "Standardize features": "Processed",
        "Train model (XGBoost)": "Trained",
    },
    "responses": {
        "DataReady": _R_DATA,
        "Processed": _R_PROCESSED,
        "Trained": _R_TRAINED,
    },
}

TEMPLATES["train-batch-transform"] = {
    "name": "credit-train-batch-transform",
    "runnable": True,
    "comment": "Generate data -> train XGBoost -> register model -> batch-score the book.",
    "input": {},
    "definition": {
        "Comment": "Train a model and batch-score applicants.",
        "StartAt": "Generate dataset",
        "States": {
            "Generate dataset": _generate("Train model (XGBoost)"),
            "Train model (XGBoost)": _training(next_state="Save model"),
            "Save model": _save_model("Batch transform"),
            "Batch transform": _batch_transform(),
        },
    },
    "testCase": "happy-path",
    "mock": {
        "Generate dataset": "DataReady",
        "Train model (XGBoost)": "Trained",
        "Save model": "Registered",
        "Batch transform": "Scored",
    },
    "responses": {
        "DataReady": _R_DATA,
        "Trained": _R_TRAINED,
        "Registered": _R_MODEL,
        "Scored": _R_TRANSFORM,
    },
}

TEMPLATES["hpo-batch-transform"] = {
    "name": "credit-hpo-batch-transform",
    "runnable": True,
    "comment": "Generate data -> hyperparameter tuning -> register best model -> batch-score.",
    "input": {},
    "definition": {
        "Comment": "Tune, then batch-score with the best model.",
        "StartAt": "Generate dataset",
        "States": {
            "Generate dataset": _generate("Tune model (XGBoost)"),
            "Tune model (XGBoost)": {
                "Type": "Task",
                "Resource": "arn:aws:states:::sagemaker:createHyperParameterTuningJob.sync",
                "Parameters": {
                    "HyperParameterTuningJobName.$": "$$.Execution.Name",
                    "HyperParameterTuningJobConfig": {
                        "Strategy": "Bayesian",
                        "HyperParameterTuningJobObjective": {
                            "Type": "Maximize",
                            "MetricName": "validation:auc",
                        },
                        "ResourceLimits": {
                            "MaxNumberOfTrainingJobs": 4,
                            "MaxParallelTrainingJobs": 2,
                        },
                        "ParameterRanges": {
                            "IntegerParameterRanges": [
                                {
                                    "Name": "max_depth",
                                    "MinValue": "3",
                                    "MaxValue": "10",
                                    "ScalingType": "Auto",
                                }
                            ]
                        },
                    },
                    "TrainingJobDefinition": {
                        "AlgorithmSpecification": {
                            "TrainingImage": IMAGE,
                            "TrainingInputMode": "File",
                        },
                        "OutputDataConfig": {"S3OutputPath": f"{S3}/models"},
                        "ResourceConfig": {
                            "InstanceCount": 1,
                            "InstanceType": "local",
                            "VolumeSizeInGB": 30,
                        },
                        "StoppingCondition": {"MaxRuntimeInSeconds": 86400},
                        "RoleArn": SM_ROLE,
                        "InputDataConfig": [
                            {
                                "ChannelName": "train",
                                "ContentType": "text/csv",
                                "DataSource": {
                                    "S3DataSource": {
                                        "S3DataType": "S3Prefix",
                                        "S3Uri": f"{S3}/train",
                                    }
                                },
                            }
                        ],
                        "StaticHyperParameters": {
                            "objective": "binary:logistic",
                            "num_round": "50",
                        },
                    },
                },
                "Next": "Save best model",
            },
            "Save best model": {
                "Type": "Task",
                "Resource": "arn:aws:states:::sagemaker:createModel",
                "Parameters": {
                    "ModelName.$": "$.bestTrainingJobName",
                    "ExecutionRoleArn": SM_ROLE,
                    "PrimaryContainer": {
                        "Image": IMAGE,
                        "ModelDataUrl.$": "$.modelDataUrl",
                    },
                },
                "Next": "Batch transform",
            },
            "Batch transform": _batch_transform(),
        },
    },
    "testCase": "happy-path",
    "mock": {
        "Generate dataset": "DataReady",
        "Tune model (XGBoost)": "Tuned",
        "Save best model": "Registered",
        "Batch transform": "Scored",
    },
    "responses": {
        "DataReady": _R_DATA,
        "Tuned": {
            "BestTrainingJob": {"TrainingJobName": "credit-xgb-trial-3"},
            "bestTrainingJobName": "credit-xgb-trial-3",
            "modelDataUrl": f"{S3}/models/credit-xgb-trial-3/output/model.tar.gz",
        },
        "Registered": _R_MODEL,
        "Scored": _R_TRANSFORM,
    },
}

# Live template: runs un-mocked against the real local model. The engine can't
# schedule bedrock:invokeModel, but it can schedule lambda:invoke against the host
# shim (oblako.lambda_shim), whose `bedrock-invoke` function forwards to Ollama.
TEMPLATES["bedrock-reason-codes"] = {
    "name": "credit-bedrock-reason-codes",
    "runnable": True,
    "note": "Runs live against your local Bedrock model (Ollama) via the lambda shim — no mock.",
    "comment": "Chain Bedrock prompts: summarize an applicant, then draft adverse-action reason codes.",
    "input": {
        "modelId": "qwen2.5:0.5b",
        "prompt": "Summarize this credit applicant in two sentences: application_score=610, "
        "bureau_score=640, debt_to_income=0.46, months_at_employer=7, residential_status=Tenant.",
    },
    "definition": {
        "Comment": "Prompt-chain a credit decision narrative against the local model.",
        "StartAt": "Summarize applicant",
        "States": {
            "Summarize applicant": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {
                    "FunctionName": "bedrock-invoke",
                    "Payload": {"modelId.$": "$.modelId", "prompt.$": "$.prompt"},
                },
                "ResultPath": "$.summary",
                "Next": "Draft reason codes",
            },
            "Draft reason codes": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {
                    "FunctionName": "bedrock-invoke",
                    "Payload": {
                        "modelId.$": "$.modelId",
                        "prompt": "Based on that summary, list the top adverse-action reason codes "
                        "as a short numbered list.",
                        "context.$": "$.summary.Payload.text",
                    },
                },
                "ResultPath": "$.reasons",
                "Next": "Done",
            },
            "Done": {"Type": "Succeed"},
        },
    },
    "testCase": None,
}


def build_mock_config(names: list[str] | None = None) -> dict:
    """Build an SFN Local MockConfigFile covering the given templates (default: all).

    Response names are namespaced by state-machine name to stay globally unique in the
    flat ``MockedResponses`` map.
    """
    want = set(names) if names is not None else None
    machines: dict[str, dict] = {}
    responses: dict[str, dict] = {}
    for tpl in TEMPLATES.values():
        sm = tpl["name"]
        if not tpl.get("mock"):  # live templates (e.g. Bedrock) run un-mocked
            continue
        if want is not None and sm not in want:
            continue
        case_map = {}
        for state, short in tpl["mock"].items():
            key = f"{sm}.{short}"
            case_map[state] = key
            responses[key] = {"0": {"Return": tpl["responses"][short]}}
        machines[sm] = {"TestCases": {tpl["testCase"]: case_map}}
    return {"StateMachines": machines, "MockedResponses": responses}


def public_templates() -> list[dict]:
    """Return the templates as JSON-serializable summaries for the dashboard/UI."""
    return [
        {
            "id": tid,
            "name": t["name"],
            "comment": t["comment"],
            "runnable": t.get("runnable", False),
            "execMode": "mock" if t.get("testCase") else "live",
            "note": t.get("note"),
            "testCase": t["testCase"],
            "input": t["input"],
            "definition": t["definition"],
        }
        for tid, t in TEMPLATES.items()
    ]
