#!/usr/bin/env python3
"""SageMaker 'bring your own container' inference server.

SageMaker (and oblako) run this image with the model mounted at /opt/ml/model and
expect an HTTP server on :8080 answering GET /ping (health) and POST /invocations
(inference). This one loads the linear model written by the training image and
predicts y = slope*x + intercept for each input x (CSV lines or a JSON list).
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

MODEL = json.load(open(os.path.join("/opt/ml/model", "model.json")))


def _predict(x: float) -> float:
    return MODEL["slope"] * x + MODEL["intercept"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        if self.path == "/ping":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802 - http.server API
        if self.path != "/invocations":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8").strip()
        if body.startswith("["):
            xs = [float(v) for v in json.loads(body)]
        else:
            xs = [float(line.split(",")[0]) for line in body.splitlines() if line]
        out = "\n".join(str(_predict(x)) for x in xs)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.end_headers()
        self.wfile.write(out.encode("utf-8"))

    def log_message(self, *args):  # silence request logging
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
