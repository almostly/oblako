#!/usr/bin/env python3
"""Serverless-MLflow SageMaker training entry point.

Fits y = slope*x + intercept by least squares (pure Python) on CSV (x,y) from
/opt/ml/input/data/train/, and tracks the run with MLflow. MLFLOW_TRACKING_URI
selects where: locally a sqlite database on /opt/ml/model (the only writable
mount, so it is collected into model.tar.gz and the experiment travels with the
model); on AWS the same code points at a SageMaker MLflow App ARN. Artifacts are
written under /opt/ml/model too, so they travel with the model as well. The
fitted model is also written to /opt/ml/model/ for serving.
"""

import glob
import json
import os

import mlflow

INPUT = "/opt/ml/input/data/train"
CONFIG = "/opt/ml/input/config/hyperparameters.json"
MODEL = "/opt/ml/model"
EXPERIMENT = "oblako-serverless"
# default: the serverless sqlite store on the writable model mount
DEFAULT_URI = "sqlite:////opt/ml/model/mlruns.db"


def _hyperparameters():
    try:
        with open(CONFIG) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def main():
    xs, ys = [], []
    for path in sorted(glob.glob(os.path.join(INPUT, "*.csv"))):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                x, y = line.split(",")
                xs.append(float(x))
                ys.append(float(y))

    n = len(xs)
    if n == 0:
        raise SystemExit("no training data found in /opt/ml/input/data/train")

    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    intercept = (sy - slope * sx) / n
    mse = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)) / n

    os.makedirs(MODEL, exist_ok=True)
    model_path = os.path.join(MODEL, "model.json")
    with open(model_path, "w") as fh:
        json.dump({"slope": slope, "intercept": intercept, "rows": n}, fh)

    hp = _hyperparameters()
    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", DEFAULT_URI))
    # keep artifacts on the collected model mount unless an explicit root is set
    if mlflow.get_experiment_by_name(EXPERIMENT) is None:
        mlflow.create_experiment(
            EXPERIMENT,
            artifact_location=os.environ.get(
                "MLFLOW_ARTIFACT_ROOT", f"file://{MODEL}/mlartifacts"
            ),
        )
    mlflow.set_experiment(EXPERIMENT)
    with mlflow.start_run() as run:
        mlflow.log_params(
            {"rows": n, "method": "least_squares", **{k: str(v) for k, v in hp.items()}}
        )
        mlflow.log_metric("mse", mse)
        mlflow.log_metric("slope", slope)
        mlflow.log_metric("intercept", intercept)
        mlflow.log_artifact(model_path)
        print(f"MLflow run {run.info.run_id}: mse={mse:.6f}, slope={slope:.4f}")

    print(f"Trained on {n} rows: y = {slope:.4f}*x + {intercept:.4f}")


if __name__ == "__main__":
    main()
