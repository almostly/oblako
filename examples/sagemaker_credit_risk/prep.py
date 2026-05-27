"""Pipeline processing step: generate the credit training data (stdlib only).

Runs in a stock python:3.11-slim container, so it has no third-party deps. Writes
the labeled training CSV to the processing output channel for the training step.
"""

import csv
import math
import os
import random

OUT = "/opt/ml/processing/output"
FEATURES = ["income", "debt_ratio", "credit_util", "num_delinquencies", "employment_years"]


def main():
    rng = random.Random(0)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "train.csv"), "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([*FEATURES, "default"])
        for _ in range(500):
            income = rng.uniform(20_000, 200_000)
            debt = rng.uniform(0, 0.8)
            util = rng.uniform(0, 1)
            delinq = rng.choice([0, 0, 0, 1, 1, 2, 3])
            emp = rng.uniform(0, 30)
            risk = 1.5 * util + 1.2 * debt + 0.5 * delinq - 4e-6 * income - 0.04 * emp
            pd = 1 / (1 + math.exp(-(risk - 1.2) * 2))  # centered for a ~18% default rate
            default = 1 if rng.random() < pd else 0
            writer.writerow([round(income), round(debt, 3), round(util, 3), delinq, round(emp, 1), default])
    print("wrote", os.path.join(OUT, "train.csv"))


if __name__ == "__main__":
    main()
