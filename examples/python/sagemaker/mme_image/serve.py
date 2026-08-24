#!/usr/bin/env python3
"""SageMaker multi-model endpoint (MME) inference server.

One container serves many models. SageMaker (and oblako) pick the model per
request via the X-Amzn-SageMaker-Target-Model header; oblako has already placed
that model's files under /opt/ml/models/<TargetModel>/ before invoking. The
server loads the target model on first use (caching it) and predicts
y = slope*x + intercept from CSV lines or a JSON list. GET /ping is health and
GET /models lists the models currently loaded.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS_DIR = "/opt/ml/models"
_loaded = {}


def _load(name):
    if name not in _loaded:
        with open(os.path.join(MODELS_DIR, name, "model.json")) as fh:
            _loaded[name] = json.load(fh)
    return _loaded[name]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.end_headers()
        elif self.path == "/models":
            self._json(200, {"models": sorted(_loaded)})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/invocations":
            self.send_response(404)
            self.end_headers()
            return
        target = self.headers.get("X-Amzn-SageMaker-Target-Model")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8").strip()
        try:
            model = _load(target)
        except (FileNotFoundError, TypeError):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(f"model not found: {target}".encode())
            return
        if body.startswith("["):
            xs = [float(v) for v in json.loads(body)]
        else:
            xs = [float(line.split(",")[0]) for line in body.splitlines() if line]
        out = "\n".join(str(model["slope"] * x + model["intercept"]) for x in xs)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.end_headers()
        self.wfile.write(out.encode())

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
