"""Train a CatBoost credit model and store it with PDO scorecard scaling.

Mirrors the reference (aws-samples/credit-risk-modeling-on-aws): CatBoostClassifier
over 7 numeric + 5 categorical application features, predicting P(default). The
score is computed at serving time from SHAP log-odds; here we just persist the
model + the scaling metadata (factor/offset) SageMaker hands us as hyperparameters.
"""

import glob
import json
import os

INPUT = "/opt/ml/input/data/train"
MODEL = "/opt/ml/model"
CONFIG = "/opt/ml/input/config/hyperparameters.json"

NUMERIC = [
    "Application_Score",
    "Bureau_Score",
    "Loan_Amount",
    "Time_with_Bank",
    "Time_in_Employment",
    "Loan_to_income",
    "Gross_Annual_Income",
]
CATEGORICAL = [
    "Loan_Payment_Frequency",
    "Residential_Status",
    "Cheque_Card_Flag",
    "Existing_Customer_Flag",
    "Home_Telephone_Number",
]
FEATURES = NUMERIC + CATEGORICAL


def run():
    """Fit CatBoost on the training channel and save the model + scorecard metadata."""
    import joblib
    import pandas as pd
    from catboost import CatBoostClassifier

    hp = json.load(open(CONFIG)) if os.path.exists(CONFIG) else {}
    df = pd.read_csv(sorted(glob.glob(os.path.join(INPUT, "*.csv")))[0])
    target = next(c for c in ("target", "is_bad", "default", "y") if c in df.columns)
    X = df[FEATURES].copy()
    for col in CATEGORICAL:
        X[col] = X[col].astype(str)
    y = df[target]

    model = CatBoostClassifier(
        iterations=int(hp.get("iterations", 200)),
        depth=int(hp.get("depth", 6)),
        learning_rate=float(hp.get("learning-rate", 0.1)),
        random_seed=42,
        verbose=False,
    )
    model.fit(X, y, cat_features=[FEATURES.index(c) for c in CATEGORICAL])

    os.makedirs(MODEL, exist_ok=True)
    joblib.dump(model, os.path.join(MODEL, "catboost_model.joblib"))
    with open(os.path.join(MODEL, "model_metadata.json"), "w") as fh:
        json.dump(
            {
                "feature_names": FEATURES,
                "categorical_features": CATEGORICAL,
                "factor": float(hp.get("factor", 20.0)),
                "offset": float(hp.get("offset", 600.0)),
            },
            fh,
        )
    print(
        f"trained CatBoost on {len(y)} rows, {len(FEATURES)} features "
        f"(default rate {y.mean():.2f})"
    )
