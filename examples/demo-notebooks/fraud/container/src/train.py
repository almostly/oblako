"""Train a linear fraud classifier (hinge-loss SVM) on the SageMaker train channel.

The original notebook used SageMaker's managed LinearLearner with ``loss=hinge_loss``
and ``positive_example_weight_mult=balanced`` — a linear SVM. This BYOC trainer is
the same model, scikit-learn's ``SGDClassifier(loss="hinge")`` with balanced class
weights, standardised features, so it runs anywhere via SageMaker local mode.
"""

import glob
import json
import os

INPUT = "/opt/ml/input/data/train"
MODEL = "/opt/ml/model"
CONFIG = "/opt/ml/input/config/hyperparameters.json"

LABEL = "Class"


def run():
    """Fit the linear SVM on the training channel and save the model + metadata."""
    import joblib
    import pandas as pd
    from sklearn.linear_model import SGDClassifier
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    hp = {}
    if os.path.exists(CONFIG):
        hp = json.load(open(CONFIG))
    epochs = int(hp.get("epochs", 20))

    csv_path = glob.glob(os.path.join(INPUT, "*.csv"))[0]
    df = pd.read_csv(csv_path)
    features = [c for c in df.columns if c != LABEL]
    x, y = df[features], df[LABEL].astype(int)

    model = make_pipeline(
        StandardScaler(),
        SGDClassifier(loss="hinge", class_weight="balanced", max_iter=epochs, tol=1e-3),
    )
    model.fit(x, y)
    print(f"trained on {len(df)} rows, {y.mean() * 100:.1f}% fraud, {epochs} epochs")

    os.makedirs(MODEL, exist_ok=True)
    joblib.dump(model, os.path.join(MODEL, "fraud_model.joblib"))
    json.dump(
        {"feature_names": features, "label": LABEL, "model": "SGDClassifier(hinge)"},
        open(os.path.join(MODEL, "model_metadata.json"), "w"),
    )
    print("saved model to", MODEL)
