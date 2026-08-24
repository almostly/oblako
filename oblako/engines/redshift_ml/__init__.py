"""Local Redshift ML: CREATE MODEL via SageMaker-local training and an in-DB plpython3u inference UDF.

`CREATE MODEL name FROM (SELECT ...) TARGET col FUNCTION fn [MODEL_TYPE ...]`:
  1. run the SELECT against the Redshift engine -> features + target rows
  2. train in a real SageMaker local container (LINEAR_LEARNER / MLP / XGBOOST)
  3. store the exported model (plain JSON) in the `_ml_models` table
  4. generate a pure-Python plpython3u UDF `fn(...)` -> in-DB inference

Problem types: regression, binary, and multiclass classification (auto-detected
from the target, or set via PROBLEM_TYPE / OBJECTIVE). With no MODEL_TYPE,
Autopilot (AUTO ON, the default) trains all three types and keeps the one with
the best holdout score; MODEL_TYPE (or AUTO OFF) pins a single type.

Inference is pure Python because the Redshift engine's plpython3u has no numpy/sklearn/
xgboost; the trained model is exported to plain numbers/trees and evaluated in
the UDF (linear: dot product / argmax; MLP: forward pass / argmax; XGBoost:
tree-walk, summing per-class for multiclass).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import tempfile

_TRAIN_DIR = pathlib.Path(__file__).parent / "train"
_TRAIN_IMAGE = "oblako-redshift-ml:latest"

_CREATE_MODEL_RE = re.compile(
    r"^\s*CREATE\s+MODEL\s+(?P<name>\w+)\s+FROM\s*\((?P<select>.+?)\)\s+"
    r"TARGET\s+(?P<target>\w+)\s+FUNCTION\s+(?P<fn>\w+)(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def is_create_model(sql: str) -> bool:
    """Return True if the SQL string starts with a CREATE MODEL statement."""
    return bool(re.match(r"\s*CREATE\s+MODEL\b", sql, re.IGNORECASE))


def parse_create_model(sql: str) -> dict:
    """Parse a CREATE MODEL SQL statement and return a spec dict with all extracted options."""
    m = _CREATE_MODEL_RE.match(sql)
    if not m:
        raise ValueError(
            "Could not parse CREATE MODEL. Expected: CREATE MODEL <name> "
            "FROM (<select>) TARGET <col> FUNCTION <fn> "
            "[MODEL_TYPE LINEAR_LEARNER|MLP|XGBOOST] [PROBLEM_TYPE ...] [OBJECTIVE ...]"
        )
    rest = m.group("rest")
    # MODEL_TYPE fixes a single algorithm; otherwise Autopilot (AUTO ON, the
    # Redshift default) trains all three and picks the best. AUTO OFF needs one.
    mt = re.search(r"MODEL_TYPE\s+'?(\w+)'?", rest, re.IGNORECASE)
    if mt:
        model_type = mt.group(1).upper()
        if model_type not in _UDF_BODIES:
            raise ValueError(
                f"unsupported MODEL_TYPE {model_type!r}; supported: {sorted(_UDF_BODIES)}"
            )
        autopilot = False
    else:
        if re.search(r"AUTO\s+OFF", rest, re.IGNORECASE):
            raise ValueError("AUTO OFF requires MODEL_TYPE LINEAR_LEARNER|MLP|XGBOOST")
        model_type, autopilot = None, True

    pt = re.search(r"PROBLEM_TYPE\s+(\w+)", rest, re.IGNORECASE)
    problem_type = None
    if pt and pt.group(1).lower() in _PROBLEM_TYPES:
        problem_type = pt.group(1).lower()
    if problem_type is None:  # Redshift's XGBoost uses OBJECTIVE instead
        obj = re.search(r"OBJECTIVE\s+'([^']+)'", rest, re.IGNORECASE)
        if obj:
            objective = obj.group(1).lower()
            if objective.startswith("binary:"):
                problem_type = "binary_classification"
            elif objective.startswith("reg:"):
                problem_type = "regression"
            elif objective.startswith("multi:"):
                problem_type = "multiclass_classification"

    num_round = re.search(r"NUM_ROUND\s+'?(\d+)'?", rest, re.IGNORECASE)
    max_depth = re.search(r"MAX_DEPTH\s+'?(\d+)'?", rest, re.IGNORECASE)
    return {
        "name": m.group("name"),
        "select": m.group("select").strip(),
        "target": m.group("target"),
        "function": m.group("fn"),
        "model_type": model_type,
        "autopilot": autopilot,
        "problem_type": problem_type,
        "num_round": int(num_round.group(1)) if num_round else None,
        "max_depth": int(max_depth.group(1)) if max_depth else None,
    }


_PROBLEM_TYPES = {"regression", "binary_classification", "multiclass_classification"}


# -------------------------------------------------------------------------------
# plpython3u inference UDF codegen (pure Python, per model type)
# -------------------------------------------------------------------------------
def _linear_body(pt: str) -> str:
    if pt == "multiclass_classification":
        return (
            'cls = m["classes"]; W = m["weights"]; b = m["intercepts"]\n'
            "scores = [sum(W[k][i] * x[i] for i in range(len(x))) + b[k] for k in range(len(cls))]\n"
            "return float(cls[max(range(len(cls)), key=lambda k: scores[k])])\n"
        )
    body = 'z = sum(w * xi for w, xi in zip(m["weights"], x)) + m["intercept"]\n'
    if pt == "binary_classification":
        return body + "return 1.0 if 1.0 / (1.0 + math.exp(-z)) >= 0.5 else 0.0\n"
    return body + "return z\n"


def _mlp_body(pt: str) -> str:
    body = """sc = m["scaler"]
x = [(xi - mu) / sd if sd else 0.0 for xi, mu, sd in zip(x, sc["mean"], sc["std"])]
def _act(v, name):
    if name == "relu":
        return v if v > 0 else 0.0
    if name == "tanh":
        return math.tanh(v)
    if name == "logistic":
        return 1.0 / (1.0 + math.exp(-v))
    return v
layers = m["layers"]
for _li, _layer in enumerate(layers):
    W = _layer["W"]; b = _layer["b"]
    z = [sum(x[i] * W[i][j] for i in range(len(x))) + b[j] for j in range(len(b))]
    _name = m["hidden_activation"] if _li < len(layers) - 1 else m["out_activation"]
    x = [_act(v, _name) for v in z]
"""
    if pt == "multiclass_classification":
        # final layer has one unit per class; argmax of the logits is the class
        return body + (
            'cls = m["classes"]\n'
            "return float(cls[max(range(len(x)), key=lambda k: x[k])])\n"
        )
    body += "out = x[0]\n"
    if pt == "binary_classification":
        return body + "return 1.0 if out >= 0.5 else 0.0\n"
    return body + 'return out * m["y_std"] + m["y_mean"]\n'


def _xgb_body(pt: str) -> str:
    if pt == "multiclass_classification":
        # softprob lays trees out round-major; tree i scores class (i % num_class)
        return """cls = m["classes"]; K = m["num_class"]
totals = [0.0] * K
for _ti, tree in enumerate(m["trees"]):
    nid = "0"
    while True:
        node = tree[nid]
        if "leaf" in node:
            totals[_ti % K] += node["leaf"]; break
        nid = str(node["y"]) if x[node["f"]] < node["c"] else str(node["n"])
return float(cls[max(range(K), key=lambda k: totals[k])])
"""
    body = """total = 0.0
for tree in m["trees"]:
    nid = "0"
    while True:
        node = tree[nid]
        if "leaf" in node:
            total += node["leaf"]
            break
        nid = str(node["y"]) if x[node["f"]] < node["c"] else str(node["n"])
bs = m["base_score"]
"""
    if pt == "binary_classification":
        return body + (
            "base_margin = math.log(bs / (1.0 - bs))\n"
            "return 1.0 if 1.0 / (1.0 + math.exp(-(base_margin + total))) >= 0.5 else 0.0\n"
        )
    return body + "return bs + total\n"


_UDF_BODIES = {"LINEAR_LEARNER": _linear_body, "MLP": _mlp_body, "XGBOOST": _xgb_body}


def _udf_sql(
    name: str, function: str, features: list[str], problem_type: str, model_type: str
) -> str:
    """Execute UDF with SQL."""
    args = ", ".join(f"{f} float" for f in features)
    header = f"""
import json, math
key = "rsml_{name}"
if key not in GD:
    rv = plpy.execute("SELECT model FROM _ml_models WHERE name = '{name}'")
    if not rv:
        plpy.error("model {name} not found")
    GD[key] = json.loads(rv[0]["model"])
m = GD[key]
x = [{", ".join(features)}]
"""
    body = _UDF_BODIES[model_type](problem_type)
    return (
        f"CREATE OR REPLACE FUNCTION {function}({args}) RETURNS float AS $$"
        f"{header}{body}$$ LANGUAGE plpython3u;"
    )


def _ensure_table(cur):
    """Ensure table exists."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _ml_models (
            name TEXT PRIMARY KEY, function_name TEXT, target TEXT, features TEXT,
            model TEXT, model_type TEXT, problem_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _train_local(X: list[list[float]], y: list[float], hyperparameters: dict) -> dict:
    """Train in a real SageMaker training container; return the exported model dict.

    oblako runs the training container itself, per SageMaker's ``/opt/ml`` contract
    (``SageMakerService.run_training``), rather than through the SDK's local mode,
    so this is independent of the SageMaker SDK version.
    """
    from oblako.services import SageMakerService

    data_dir = tempfile.mkdtemp(prefix="rsml-train-")
    try:
        with open(os.path.join(data_dir, "train.csv"), "w") as fh:
            for row, target in zip(X, y):
                fh.write(",".join(str(v) for v in row) + "," + str(target) + "\n")
        sm = SageMakerService()
        sm.build_image(path=str(_TRAIN_DIR), tag=_TRAIN_IMAGE)
        files = sm.run_training(
            image=_TRAIN_IMAGE,
            channels={"train": data_dir},
            hyperparameters=hyperparameters,
        )
        return json.loads(files["model.json"])
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def create_model(
    spec: dict, *, host: str, port: int, user: str, password: str, database: str
) -> dict:
    """Train a model from a CREATE MODEL spec and register its inference UDF."""
    import psycopg2

    conn = psycopg2.connect(
        host=host, port=port, user=user, password=password, dbname=database
    )
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(spec["select"])
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        target = spec["target"]
        if target not in columns:
            raise ValueError(
                f"TARGET {target!r} is not in the SELECT columns {columns}"
            )
        feature_idx = [i for i, c in enumerate(columns) if c != target]
        target_idx = columns.index(target)
        features = [columns[i] for i in feature_idx]
        X = [[float(r[i]) for i in feature_idx] for r in rows]
        y = [float(r[target_idx]) for r in rows]
        if not X:
            raise ValueError("the FROM (SELECT ...) returned no rows to train on")

        problem_type = spec["problem_type"] or _detect_problem_type(y)

        base_hp = {"problem_type": problem_type}
        if spec.get("num_round"):
            base_hp["num_round"] = str(spec["num_round"])
        if spec.get("max_depth"):
            base_hp["max_depth"] = str(spec["max_depth"])

        leaderboard = None
        if spec.get("autopilot"):
            # Autopilot: train every type, keep the one with the best holdout score.
            best = None
            leaderboard = []
            for candidate in ("XGBOOST", "MLP", "LINEAR_LEARNER"):
                trained = _train_local(
                    X, y, dict(base_hp, model_type=candidate, autopilot="true")
                )
                score = trained.get("val_score", float("-inf"))
                leaderboard.append({"model_type": candidate, "val_score": score})
                if best is None or score > best[0]:
                    best = (score, candidate, trained)
            _, model_type, model = best
        else:
            model_type = spec["model_type"]
            model = _train_local(X, y, dict(base_hp, model_type=model_type))

        model["features"] = features

        _ensure_table(cur)
        cur.execute("DELETE FROM _ml_models WHERE name = %s", (spec["name"],))
        cur.execute(
            "INSERT INTO _ml_models (name, function_name, target, features, model, model_type, problem_type) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                spec["name"],
                spec["function"],
                target,
                json.dumps(features),
                json.dumps(model),
                model_type,
                problem_type,
            ),
        )
        cur.execute(
            _udf_sql(spec["name"], spec["function"], features, problem_type, model_type)
        )
        cur.close()
        result = {
            "model": spec["name"],
            "function": spec["function"],
            "features": features,
            "model_type": model_type,
            "problem_type": problem_type,
            "rows": len(y),
        }
        if leaderboard is not None:
            result["autopilot"] = sorted(
                leaderboard, key=lambda r: r["val_score"], reverse=True
            )
            result["selected"] = model_type
        return result
    finally:
        conn.close()


def _detect_problem_type(y: list[float]) -> str:
    """0/1 -> binary; small non-negative integer set -> multiclass; else regression."""
    uniq = set(y)
    if uniq <= {0.0, 1.0}:
        return "binary_classification"
    if all(float(v).is_integer() and v >= 0 for v in y) and 3 <= len(uniq) <= 20:
        return "multiclass_classification"
    return "regression"
