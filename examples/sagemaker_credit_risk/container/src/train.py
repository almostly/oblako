"""Train a credit-default classifier and bake in PDO scorecard scaling.

Reads CSV training data (feature columns + a final 0/1 `default` column) from
SageMaker's input channel, fits a logistic-regression PD model, and writes the
model + scorecard metadata (factor/offset) to the model dir. Hyperparameters
(target score/odds, points-to-double-odds, cutoff) come from SageMaker.
"""

import csv
import glob
import json
import math
import os

INPUT = "/opt/ml/input/data/train"
MODEL = "/opt/ml/model"
CONFIG = "/opt/ml/input/config/hyperparameters.json"


def _load():
    X, y, header = [], [], []
    for path in sorted(glob.glob(os.path.join(INPUT, "*.csv"))):
        with open(path) as fh:
            reader = csv.reader(fh)
            header = next(reader)
            for row in reader:
                *feats, label = row
                X.append([float(v) for v in feats])
                y.append(int(float(label)))
    if not X:
        raise SystemExit(f"no training CSVs in {INPUT}")
    return X, y, header[:-1]


def run():
    """Fit the PD model, compute the scorecard scaling, and save the artifacts."""
    from sklearn.linear_model import LogisticRegression

    hp = json.load(open(CONFIG)) if os.path.exists(CONFIG) else {}
    X, y, features = _load()
    model = LogisticRegression(max_iter=1000).fit(X, y)

    # PDO scorecard scaling: score = offset + factor * ln(odds), odds = P(good)/P(bad).
    target_score = float(hp.get("target-score", 600))
    target_odds = float(hp.get("target-odds", 30))
    pdo = float(hp.get("pts-double-odds", 20))
    factor = pdo / math.log(2)
    offset = target_score - factor * math.log(target_odds)

    os.makedirs(MODEL, exist_ok=True)
    import joblib

    joblib.dump(model, os.path.join(MODEL, "model.joblib"))
    with open(os.path.join(MODEL, "model_metadata.json"), "w") as fh:
        json.dump({"features": features, "factor": factor, "offset": offset,
                   "cutoff": float(hp.get("cutoff", target_score))}, fh)
    print(f"trained on {len(y)} rows, {len(features)} features "
          f"(default rate {sum(y) / len(y):.2f}); factor={factor:.1f} offset={offset:.1f}")
