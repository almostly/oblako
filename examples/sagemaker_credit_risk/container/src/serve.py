"""Serve credit scores over SageMaker's container contract (GET /ping, POST /invocations).

Loads the trained PD model + scorecard metadata, turns each application's
probability of default into a scorecard score, and returns an APPROVE/DECLINE
decision against the cutoff.
"""

import json
import math
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

MODEL = "/opt/ml/model"
_model = None
_meta = None


def _load():
    global _model, _meta
    import joblib

    _model = joblib.load(os.path.join(MODEL, "model.joblib"))
    _meta = json.load(open(os.path.join(MODEL, "model_metadata.json")))


def _score(instances):
    feats, factor, offset, cutoff = (_meta["features"], _meta["factor"], _meta["offset"], _meta["cutoff"])
    rows = [[float(inst[f]) for f in feats] for inst in instances]
    out = []
    for proba in _model.predict_proba(rows):
        pd = min(max(float(proba[1]), 1e-6), 1 - 1e-6)  # P(default)
        score = int(round(offset + factor * math.log((1 - pd) / pd)))
        out.append({"pd": round(pd, 4), "score": score,
                    "decision": "APPROVE" if score >= cutoff else "DECLINE"})
    return out


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - SageMaker health check
        self.send_response(200 if self.path == "/ping" else 404)
        self.end_headers()

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
    """Load the model and serve the scoring HTTP contract on :8080."""
    _load()
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
