"""Synthetic credit-application data for the SageMaker credit-risk example."""

import csv
import math
import random

FEATURES = ["income", "debt_ratio", "credit_util", "num_delinquencies", "employment_years"]


def _row(rng):
    income = rng.uniform(20_000, 200_000)
    debt_ratio = rng.uniform(0.0, 0.8)
    credit_util = rng.uniform(0.0, 1.0)
    num_delinq = rng.choice([0, 0, 0, 1, 1, 2, 3])
    employment_years = rng.uniform(0, 30)
    # latent risk: high utilization/debt/delinquencies + low income/tenure -> default
    risk = (1.5 * credit_util + 1.2 * debt_ratio + 0.5 * num_delinq
            - 4e-6 * income - 0.04 * employment_years)
    pd = 1 / (1 + math.exp(-(risk - 1.2) * 2))  # centered for a ~18% default rate
    default = 1 if rng.random() < pd else 0
    return [round(income), round(debt_ratio, 3), round(credit_util, 3), num_delinq,
            round(employment_years, 1)], default


def write_training_csv(path, n=500, seed=0):
    """Write n labeled credit applications (features + 0/1 default) to a CSV."""
    rng = random.Random(seed)
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([*FEATURES, "default"])
        for _ in range(n):
            feats, label = _row(rng)
            writer.writerow([*feats, label])


def sample_applications():
    """Return a clearly-prime and a clearly-subprime applicant to score."""
    return [
        {"income": 150000, "debt_ratio": 0.10, "credit_util": 0.05,
         "num_delinquencies": 0, "employment_years": 12},
        {"income": 28000, "debt_ratio": 0.70, "credit_util": 0.95,
         "num_delinquencies": 3, "employment_years": 0.5},
    ]
