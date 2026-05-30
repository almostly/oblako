"""Synthetic credit-card fraud transactions for the SageMaker local-mode example.

Mirrors the Kaggle creditcard.csv schema the original LinearLearner notebook used:
``Time``, anonymised ``V1``..``V28`` principal components, ``Amount``, and a binary
``Class`` (1 = fraud). Fraud is driven by a fixed linear combination of a few V
components plus amount, so a *linear* classifier (hinge-loss SVM) can learn it.
"""

import csv
import math
import random

# 30 features, exactly like the real dataset: Time, V1..V28, Amount.
FEATURES = ["Time", *[f"V{i}" for i in range(1, 29)], "Amount"]
LABEL = "Class"

# A sparse linear "fraud direction" over the V components (the signal a linear
# model recovers); everything else is noise.
_WEIGHTS = {"V3": -1.3, "V4": 1.6, "V10": -1.1, "V12": -1.4, "V14": -1.7, "V17": -1.2}


def _row(rng):
    v = {f"V{i}": rng.gauss(0, 1) for i in range(1, 29)}
    amount = round(math.exp(rng.gauss(3.0, 1.2)), 2)  # lognormal, like real amounts
    logit = -3.2 + 0.0006 * amount + sum(w * v[k] for k, w in _WEIGHTS.items())
    is_fraud = 1 if rng.random() < 1 / (1 + math.exp(-logit)) else 0
    row = {"Time": round(rng.uniform(0, 172_792)), "Amount": amount, LABEL: is_fraud}
    row.update({k: round(val, 6) for k, val in v.items()})
    return row


def write_training_csv(path, n=4000, seed=0):
    """Write n labeled transactions (30 features + ``Class``) to a CSV at ``path``."""
    rng = random.Random(seed)
    cols = [*FEATURES, LABEL]
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for _ in range(n):
            writer.writerow(_row(rng))


def fraud_rate(path):
    """Return the share of fraud (``Class == 1``) rows in a generated CSV."""
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    return sum(int(r[LABEL]) for r in rows) / max(len(rows), 1)
