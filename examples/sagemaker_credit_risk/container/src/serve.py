"""Serve the CatBoost scorecard over SageMaker's contract (GET /ping, POST /invocations).

For each application: P(default) from the model, and a credit score from CatBoost's
native SHAP values — `score = offset + factor * (-log_odds)`, where log_odds is the
sum of the SHAP feature contributions plus the base value (so higher score = better
credit). Mirrors the reference's inference.py.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

MODEL = "/opt/ml/model"
_model = None
_meta = None


def _load():
    global _model, _meta
    import joblib

    _model = joblib.load(os.path.join(MODEL, "catboost_model.joblib"))
    _meta = json.load(open(os.path.join(MODEL, "model_metadata.json")))


def _score(instances):
    import pandas as pd
    from catboost import Pool

    feats, cats = _meta["feature_names"], _meta["categorical_features"]
    factor, offset = _meta["factor"], _meta["offset"]
    cat_idx = [feats.index(c) for c in cats]
    out = []
    for inst in instances:
        X = pd.DataFrame([{f: inst.get(f, 0) for f in feats}])
        for col in cats:
            X[col] = X[col].astype(str)
        pool = Pool(X, cat_features=cat_idx)
        proba = float(_model.predict_proba(pool)[0, 1])           # P(default)
        shap = _model.get_feature_importance(type="ShapValues", data=pool)
        log_odds = float(shap[0, :-1].sum() + shap[0, -1])        # contributions + base
        out.append({"proba": round(proba, 4), "score": int(offset + factor * (-log_odds))})
    return out


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - SageMaker health check
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "healthy"}')

    def do_POST(self):  # noqa: N802 - SageMaker /invocations
        if self.path != "/invocations":
            self.send_response(404)
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        req = json.loads(body or b"{}")
        instances = req.get("instances", req) if isinstance(req, dict) else req
        payload = json.dumps({"predictions": _score(instances)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def run():
    """Load the model and serve the scoring contract on :8080."""
    _load()
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
