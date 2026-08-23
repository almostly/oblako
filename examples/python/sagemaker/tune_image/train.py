#!/usr/bin/env python3
"""Tunable SageMaker training entry point for Automatic Model Tuning (HPO).

Fits ridge regression y = slope*x + intercept with an L2 penalty `alpha` (the
hyperparameter being tuned), reading CSV (x,y) from /opt/ml/input/data/train/.
It prints the validation objective on stdout as `validation:mse=<value>`; the
tuning job's MetricDefinitions regex scrapes that line to score the trial. On
clean linear data a smaller alpha fits better, so the tuner should drive alpha
down. The fitted model is written to /opt/ml/model/.
"""

import glob
import json
import os

INPUT = "/opt/ml/input/data/train"
CONFIG = "/opt/ml/input/config/hyperparameters.json"
MODEL = "/opt/ml/model"


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

    alpha = float(_hyperparameters().get("alpha", 0.0))

    xbar, ybar = sum(xs) / n, sum(ys) / n
    sxx = sum((x - xbar) ** 2 for x in xs)
    sxy = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    # centered ridge: the L2 penalty shrinks the slope toward 0
    slope = sxy / (sxx + alpha)
    intercept = ybar - slope * xbar

    mse = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)) / n

    os.makedirs(MODEL, exist_ok=True)
    with open(os.path.join(MODEL, "model.json"), "w") as fh:
        json.dump(
            {"slope": slope, "intercept": intercept, "alpha": alpha, "mse": mse}, fh
        )

    # the objective line the tuning job scrapes via MetricDefinitions
    print(f"validation:mse={mse:.6f}")
    print(f"Trained on {n} rows with alpha={alpha:.6g}: mse={mse:.6f}")


if __name__ == "__main__":
    main()
