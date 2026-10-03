"""Redshift ML inside redshift-local: CREATE / SHOW / DROP MODEL from any client.

    CREATE MODEL [schema.]name
    FROM { table | (select) }
    TARGET col  FUNCTION fn  IAM_ROLE { default | 'arn' }
    [AUTO ON | OFF] [MODEL_TYPE ...] [PROBLEM_TYPE ...] [OBJECTIVE '...']
    [PREPROCESSORS 'none'] [HYPERPARAMETERS DEFAULT [EXCEPT (k 'v', ...)]]
    [SETTINGS (S3_BUCKET '...', MAX_RUNTIME n, ...)]

It behaves the way Redshift does:

1. The wire proxy rewrites CREATE/SHOW/DROP MODEL into calls to the plpython3u
   functions in initdb.d/09_redshift_ml.sql (``rewrite_ml``).
2. CREATE MODEL validates synchronously (clauses, the 500-row minimum for AUTO
   OFF, the target column, numeric features), records the model as TRAINING in
   ``pg_oblako.models`` and returns. Training is asynchronous.
3. The agent (``python3 redshift_ml.py agent``, started as root by the entrypoint
   next to the proxy) picks up TRAINING models and runs a real training container
   on the host Docker daemon through the mounted /var/run/docker.sock, following
   SageMaker's /opt/ml contract. PostgreSQL itself runs as ``postgres`` and can't
   use the socket, which is why training lives in a separate process.
4. The trained model is exported to plain JSON and evaluated by a generated
   pure-Python prediction function (the engine's plpython3u has no numpy or
   xgboost), plus ``<fn>_probabilities`` for AUTO ON classification models.
   ``svv_ml_model_info`` then reads ``Model is Ready``, or the failure reason.

Everything at module level is stdlib-only, so the host package and the unit tests
load this same file; the agent imports docker and psycopg lazily.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tarfile
import threading
import time
import uuid
from typing import Any

MIN_ROWS = 500  # Redshift ML's training-set minimum (enforced for AUTO OFF)
MODEL_TYPES = ("XGBOOST", "MLP", "LINEAR_LEARNER")
PROBLEM_TYPES = {"regression", "binary_classification", "multiclass_classification"}
# XGBoost objectives the local trainer and prediction functions implement
XGB_OBJECTIVES = {
    "reg:squarederror": "regression",
    "reg:squaredlogerror": "regression",
    "reg:pseudohubererror": "regression",
    "reg:logistic": "regression",
    "binary:logistic": "binary_classification",
    "multi:softmax": "multiclass_classification",
    "multi:softprob": "multiclass_classification",
}
TRAIN_IMAGE = "oblako-redshift-ml:train"
TRAIN_CONTEXT = "/usr/local/share/oblako/ml_train"
DOCKER_SOCKET = "/var/run/docker.sock"
NUMERIC_OIDS = {16, 20, 21, 23, 700, 701, 1700}  # bool, int8/2/4, float4/8, numeric

READY = "READY"
TRAINING = "TRAINING"
FAILED = "FAILED"


class MLError(ValueError):
    """A CREATE/SHOW/DROP MODEL statement Redshift would reject."""


# -----------------------------------------------------------------------------
# Parsing
# -----------------------------------------------------------------------------
_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
_QUALIFIED = rf"{_IDENT}(?:\s*\.\s*{_IDENT})?"
_CREATE = re.compile(rf"(?is)^\s*create\s+model\s+({_QUALIFIED})\s+from\s+")
_SHOW = re.compile(rf"(?is)^\s*show\s+model\s+(all|{_QUALIFIED})\s*;?\s*$")
_DROP = re.compile(rf"(?is)^\s*drop\s+model\s+(if\s+exists\s+)?({_QUALIFIED})\s*;?\s*$")


def _unquote(ident: str) -> str:
    """Redshift identifiers: quoted keeps case, unquoted folds to lower case."""
    ident = ident.strip()
    if ident.startswith('"'):
        return ident[1:-1].replace('""', '"')
    return ident.lower()


def split_name(qualified: str) -> tuple[str | None, str]:
    """``schema.name`` -> (schema, name); unqualified -> (None, name)."""
    parts = re.findall(_IDENT, qualified)
    if len(parts) == 2:
        return _unquote(parts[0]), _unquote(parts[1])
    return None, _unquote(parts[0])


def _skip_quoted(s: str, i: int) -> int:
    """Index past the quoted string or identifier starting at ``s[i]``."""
    q, n, i = s[i], len(s), i + 1
    while i < n:
        if s[i] == q:
            if i + 1 < n and s[i + 1] == q:
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _balanced(s: str, i: int) -> tuple[str, int]:
    """From ``s[i] == '('``: (inner text, index past the matching ')')."""
    depth, start, n = 0, i, len(s)
    while i < n:
        c = s[i]
        if c in "'\"":
            i = _skip_quoted(s, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[start + 1 : i], i + 1
        i += 1
    raise MLError("unbalanced parentheses in CREATE MODEL")


def _kv_pairs(text: str) -> dict[str, str]:
    """``k 'v', k2 3`` -> {"k": "v", "k2": "3"} (keys lower-cased)."""
    pairs = re.findall(r"(\w+)\s+('(?:[^']|'')*'|[^,\s)]+)", text)
    out = {}
    for key, value in pairs:
        if value.startswith("'"):
            value = value[1:-1].replace("''", "'")
        out[key.lower()] = value
    return out


def is_create_model(sql: str) -> bool:
    """Return True if the SQL string is a CREATE MODEL statement."""
    return bool(re.match(r"(?is)\s*create\s+model\b", sql))


def parse_create_model(sql: str) -> dict:
    """Parse and validate a CREATE MODEL statement into a spec dict."""
    sql = sql.strip().rstrip(";").strip()
    m = _CREATE.match(sql)
    if not m:
        raise MLError(
            "Could not parse CREATE MODEL. Expected: CREATE MODEL <name> "
            "FROM { <table> | (<select>) } TARGET <col> FUNCTION <fn> ..."
        )
    schema, name = split_name(m.group(1))
    pos = m.end()
    if sql[pos] == "(":
        select, pos = _balanced(sql, pos)
        select = select.strip()
    else:
        table = re.match(_QUALIFIED, sql[pos:])
        if not table:
            raise MLError("CREATE MODEL: FROM must name a table or a (SELECT ...)")
        select = f"SELECT * FROM {table.group(0)}"
        pos += table.end()
    rest = sql[pos:]

    def clause(pattern: str):
        return re.search(pattern, rest, re.IGNORECASE | re.DOTALL)

    target = clause(rf"\btarget\s+({_IDENT})")
    function = clause(rf"\bfunction\s+({_QUALIFIED})")
    if not target or not function:
        raise MLError("CREATE MODEL requires TARGET <column> and FUNCTION <name>")
    fn_schema, fn_name = split_name(function.group(1))

    auto = clause(r"\bauto\s+(on|off)\b")
    auto_on = not (auto and auto.group(1).lower() == "off")
    mt = clause(r"\bmodel_type\s+'?(\w+)'?")
    model_type = mt.group(1).upper() if mt else None
    if model_type and model_type not in MODEL_TYPES:
        raise MLError(
            f"unsupported MODEL_TYPE {model_type!r}; supported: {', '.join(MODEL_TYPES)}"
        )
    pt = clause(r"\bproblem_type\s+'?(\w+)'?")
    problem_type = pt.group(1).lower() if pt else None
    if problem_type and problem_type not in PROBLEM_TYPES:
        raise MLError(f"unsupported PROBLEM_TYPE {problem_type!r}")
    obj = clause(r"\bobjective\s+'([^']+)'")
    objective = obj.group(1).lower() if obj else None
    pre = clause(r"\bpreprocessors\s+'((?:[^']|'')*)'")
    preprocessors = pre.group(1).replace("''", "'") if pre else None
    role = clause(r"\biam_role\s+(default|'(?:[^']|'')*')")
    iam_role = (role.group(1).strip("'") if role else None) or None

    hyperparameters: dict[str, str] | None = None
    hp = clause(r"\bhyperparameters\s+default\b")
    if hp:
        hyperparameters = {}
        ex = re.match(r"\s*except\s*\(", rest[hp.end() :], re.IGNORECASE)
        if ex:
            inner, _ = _balanced(rest, hp.end() + ex.end() - 1)
            hyperparameters = _kv_pairs(inner)
    settings: dict[str, str] = {}
    st = clause(r"\bsettings\s*\(")
    if st:
        inner, _ = _balanced(rest, st.end() - 1)
        settings = _kv_pairs(inner)

    if not auto_on:
        # AUTO OFF skips Autopilot, so nothing picks these for you
        if model_type is None:
            raise MLError("AUTO OFF requires MODEL_TYPE XGBOOST")
        if model_type != "XGBOOST":
            raise MLError("AUTO OFF supports only MODEL_TYPE XGBOOST")
        if objective is None:
            raise MLError("AUTO OFF with MODEL_TYPE XGBOOST requires OBJECTIVE")
        if hyperparameters is None:
            raise MLError("AUTO OFF requires HYPERPARAMETERS")
        if preprocessors is None:
            raise MLError("AUTO OFF requires PREPROCESSORS")
    if preprocessors is not None and preprocessors.strip().lower() != "none":
        raise MLError(
            "oblako's local Redshift ML supports only PREPROCESSORS 'none'; "
            "prepare the features in the SELECT"
        )
    if objective is not None:
        if objective not in XGB_OBJECTIVES:
            raise MLError(
                f"unsupported OBJECTIVE {objective!r}; supported locally: "
                + ", ".join(sorted(XGB_OBJECTIVES))
            )
        problem_type = problem_type or XGB_OBJECTIVES[objective]

    return {
        "schema": schema,
        "name": name,
        "select": select,
        "target": _unquote(target.group(1)),
        "function_schema": fn_schema,
        "function": fn_name,
        "auto": auto_on,
        # MODEL_TYPE pins one algorithm; without it Autopilot tries all of them
        "model_type": model_type,
        "autopilot": model_type is None,
        "problem_type": problem_type,
        "objective": objective,
        "hyperparameters": hyperparameters or {},
        "settings": settings,
        "iam_role": iam_role,
        "sql": sql,
    }


def detect_problem_type(y: list[float]) -> str:
    """0/1 -> binary; small non-negative integer set -> multiclass; else regression."""
    uniq = set(y)
    if uniq <= {0.0, 1.0}:
        return "binary_classification"
    if all(float(v).is_integer() and v >= 0 for v in y) and 3 <= len(uniq) <= 20:
        return "multiclass_classification"
    return "regression"


# -----------------------------------------------------------------------------
# Prediction-function codegen (pure Python, evaluated by plpython3u)
# -----------------------------------------------------------------------------
_SOFTMAX = """def _softmax(v):
    top = max(v)
    e = [math.exp(t - top) for t in v]
    s = sum(e)
    return [t / s for t in e]
"""


def linear_body(pt: str, probabilities: bool = False) -> str:
    """Linear learner: dot product (+ sigmoid / softmax for classification)."""
    if pt == "multiclass_classification":
        body = (
            'cls = m["classes"]; W = m["weights"]; b = m["intercepts"]\n'
            "scores = [sum(W[k][i] * x[i] for i in range(len(x))) + b[k] "
            "for k in range(len(cls))]\n"
        )
        if probabilities:
            return _SOFTMAX + body + "return _probs(_softmax(scores), cls)\n"
        return body + (
            "return float(cls[max(range(len(cls)), key=lambda k: scores[k])])\n"
        )
    body = 'z = sum(w * xi for w, xi in zip(m["weights"], x)) + m["intercept"]\n'
    if pt == "binary_classification":
        body += "p = 1.0 / (1.0 + math.exp(-z))\n"
        if probabilities:
            return body + "return _probs([1.0 - p, p], [0.0, 1.0])\n"
        return body + "return 1.0 if p >= 0.5 else 0.0\n"
    return body + "return z\n"


def mlp_body(pt: str, probabilities: bool = False) -> str:
    """Multilayer perceptron: scaled forward pass."""
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
        # the final layer has one unit per class; argmax of the logits is the class
        if probabilities:
            return _SOFTMAX + body + 'return _probs(_softmax(x), m["classes"])\n'
        return body + (
            'cls = m["classes"]\n'
            "return float(cls[max(range(len(x)), key=lambda k: x[k])])\n"
        )
    body += "out = x[0]\n"
    if pt == "binary_classification":
        if probabilities:
            return body + "return _probs([1.0 - out, out], [0.0, 1.0])\n"
        return body + "return 1.0 if out >= 0.5 else 0.0\n"
    return body + 'return out * m["y_std"] + m["y_mean"]\n'


def xgb_body(pt: str, probabilities: bool = False) -> str:
    """XGBoost: walk every tree, add the leaves to the base margin."""
    walk = """def _leaf(tree):
    nid = "0"
    while True:
        node = tree[nid]
        if "leaf" in node:
            return node["leaf"]
        v = x[node["f"]]
        if v != v:  # NaN (a NULL feature) takes the tree's missing branch
            nid = str(node.get("m", node["n"]))
        else:
            nid = str(node["y"]) if v < node["c"] else str(node["n"])
"""
    if pt == "multiclass_classification":
        # softprob lays trees out round-major: tree i scores class (i % num_class)
        body = walk + (
            'cls = m["classes"]; K = m["num_class"]\n'
            "totals = [0.0] * K\n"
            'for _ti, tree in enumerate(m["trees"]):\n'
            "    totals[_ti % K] += _leaf(tree)\n"
        )
        if probabilities:
            return _SOFTMAX + body + "return _probs(_softmax(totals), cls)\n"
        return body + "return float(cls[max(range(K), key=lambda k: totals[k])])\n"
    body = walk + (
        'total = sum(_leaf(tree) for tree in m["trees"])\n'
        'bs = m["base_score"]\n'
        "p = 1.0 / (1.0 + math.exp(-(math.log(bs / (1.0 - bs)) + total)))\n"
    )
    if pt == "binary_classification":
        if probabilities:
            return body + "return _probs([1.0 - p, p], [0.0, 1.0])\n"
        return body + "return 1.0 if p >= 0.5 else 0.0\n"
    # regression: reg:logistic predicts a probability, the others the raw value
    return body + 'return p if m.get("objective") == "reg:logistic" else bs + total\n'


UDF_BODIES = {"LINEAR_LEARNER": linear_body, "MLP": mlp_body, "XGBOOST": xgb_body}

_PROBS = """def _probs(ps, labels):
    lab = [str(int(v)) if float(v).is_integer() else str(v) for v in labels]
    return json.dumps({"probabilities": [round(p, 6) for p in ps], "labels": lab})
"""


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def udf_sql(
    schema: str,
    function: str,
    version: str,
    n_features: int,
    problem_type: str,
    model_type: str,
    probabilities: bool = False,
    returns: str = "float8",
) -> str:
    """CREATE FUNCTION for the prediction (or ``_probabilities``) function.

    Arguments are positional and unnamed (plpython3u exposes them as ``args``),
    like Redshift's generated function: a feature called ``desc`` or ``class``
    can't break it. A NULL argument becomes NaN.
    """
    arg_types = ", ".join(["float8"] * n_features)
    header = f"""
import json, math
key = "rsml_{version}"
if key not in GD:
    rv = plpy.execute("SELECT model FROM pg_oblako.models WHERE version = '{version}'")
    if not rv:
        plpy.error("Redshift ML model for {function} not found (dropped?)")
    GD[key] = json.loads(rv[0]["model"])
m = GD[key]
x = [float("nan") if v is None else float(v) for v in args]
"""
    body = UDF_BODIES[model_type](problem_type, probabilities)
    if probabilities:
        header += _PROBS
    name = f"{function}_probabilities" if probabilities else function
    return (
        f"CREATE OR REPLACE FUNCTION {_q(schema)}.{_q(name)}({arg_types}) "
        f"RETURNS {returns} STABLE AS $oblako_udf${header}{body}$oblako_udf$ "
        "LANGUAGE plpython3u"
    )


# -----------------------------------------------------------------------------
# Wire-proxy rewrite: CREATE / SHOW / DROP MODEL -> oblako_ml_* calls
# -----------------------------------------------------------------------------
def _dollar(text: str) -> str:
    tag = "oblako_ml"
    while f"${tag}$" in text:
        tag += "_"
    return f"${tag}${text}${tag}$"


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _statements(sql: str) -> list[str]:
    """Split on top-level ';' (quote-, dollar-quote- and paren-aware)."""
    out, start, i, n, depth = [], 0, 0, len(sql), 0
    while i < n:
        c = sql[i]
        if c in "'\"":
            i = _skip_quoted(sql, i)
            continue
        if c == "$":
            tag = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if tag:
                end = sql.find(tag.group(0), i + len(tag.group(0)))
                i = n if end < 0 else end + len(tag.group(0))
                continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == ";" and depth == 0:
            out.append(sql[start : i + 1])
            start = i + 1
        i += 1
    out.append(sql[start:])
    return out


def _rewrite_one(stmt: str) -> str:
    body = stmt.rstrip()
    semi = ";" if body.endswith(";") else ""
    core = body.rstrip(";")
    lead = core[: len(core) - len(core.lstrip())]
    if is_create_model(core):
        return f"{lead}SELECT oblako_ml_create_model({_dollar(core.strip())}){semi}"
    m = _SHOW.match(core)
    if m:
        target = m.group(1)
        if target.lower() == "all":
            return f"{lead}SELECT * FROM oblako_ml_show_models(){semi}"
        return f"{lead}SELECT * FROM oblako_ml_show_model({_literal(target)}){semi}"
    m = _DROP.match(core)
    if m:
        if_exists = "true" if m.group(1) else "false"
        return (
            f"{lead}SELECT oblako_ml_drop_model({_literal(m.group(2))}, {if_exists})"
            f"{semi}"
        )
    return stmt


def rewrite_ml(sql: str) -> str:
    """Rewrite CREATE/SHOW/DROP MODEL statements; leave everything else as-is."""
    low = sql.lower()
    if "model" not in low or not re.search(r"(?i)\b(create|show|drop)\s+model\b", sql):
        return sql
    return "".join(_rewrite_one(s) for s in _statements(sql))


# -----------------------------------------------------------------------------
# plpython3u entry points (run inside PostgreSQL through SPI)
# -----------------------------------------------------------------------------
def _resolve(plpy, qualified: str) -> tuple[str, str]:
    schema, name = split_name(qualified)
    if schema is None:
        schema = plpy.execute("SELECT current_schema() AS s")[0]["s"]
    return schema, name


def _find(plpy, schema: str, name: str):
    plan = plpy.prepare(
        "SELECT * FROM pg_oblako.models WHERE schema_name = $1 AND model_name = $2",
        ["text", "text"],
    )
    rows = plan.execute([schema, name])
    return rows[0] if rows else None


def create_model(plpy, stmt: str) -> None:
    """Validate CREATE MODEL and queue it for training (Redshift is async too)."""
    try:
        spec = parse_create_model(stmt)
    except MLError as err:
        plpy.error(str(err))
    schema = spec["schema"] or plpy.execute("SELECT current_schema() AS s")[0]["s"]
    fn_schema = spec["function_schema"] or schema
    if fn_schema != schema:
        plpy.error("FUNCTION must be created in the model's schema")
    if not plpy.execute(
        f"SELECT 1 FROM pg_namespace WHERE nspname = {_literal(schema)}"
    ):
        plpy.error(f'schema "{schema}" does not exist')
    if _find(plpy, schema, spec["name"]):
        plpy.error(f'Model "{schema}.{spec["name"]}" already exists')
    if not os.path.exists(DOCKER_SOCKET):
        plpy.error(
            "Redshift ML training needs Docker: run redshift-local with "
            "/var/run/docker.sock mounted (a Docker, Podman or Colima backend)"
        )

    probe = plpy.execute(f"SELECT * FROM ({spec['select']}) AS _oblako_ml LIMIT 0")
    columns = list(probe.colnames())
    types = dict(zip(columns, probe.coltypes()))
    if spec["target"] not in columns:
        plpy.error(
            f'TARGET column "{spec["target"]}" is not in the training query '
            f"(columns: {', '.join(columns)})"
        )
    features = [c for c in columns if c != spec["target"]]
    if not features:
        plpy.error("the training query has no feature columns besides the target")
    non_numeric = [c for c in features if types[c] not in NUMERIC_OIDS]
    if non_numeric:
        plpy.error(
            "oblako's local Redshift ML needs numeric features; cast these in the "
            f"SELECT: {', '.join(non_numeric)}"
        )
    signature = ", ".join(["float8"] * len(features))
    for fn in (spec["function"], spec["function"] + "_probabilities"):
        existing = plpy.execute(
            "SELECT to_regprocedure("
            + _literal(f"{_q(schema)}.{_q(fn)}({signature})")
            + ") IS NOT NULL AS taken"
        )
        if existing[0]["taken"]:
            plpy.error(
                f'function "{schema}.{fn}" already exists with the same argument '
                "types; drop it or choose another FUNCTION name"
            )
    rows = plpy.execute(f"SELECT count(*) AS n FROM ({spec['select']}) AS _oblako_ml")
    n_rows = int(rows[0]["n"])
    if not spec["auto"] and n_rows < MIN_ROWS:
        plpy.error(
            f"the training query returned {n_rows} rows; CREATE MODEL needs at "
            f"least {MIN_ROWS}"
        )
    if n_rows == 0:
        plpy.error("the training query returned no rows")

    plan = plpy.prepare(
        "INSERT INTO pg_oblako.models (schema_name, model_name, owner, function_name, "
        "target, query, features, spec, model_state, version, training_job_name) "
        "VALUES ($1, $2, current_user, $3, $4, $5, $6, $7, $8, $9, $10)",
        ["text"] * 10,
    )
    stamp = time.strftime("%Y%m%d%H%M%S")
    plan.execute(
        [
            schema,
            spec["name"],
            spec["function"],
            spec["target"],
            spec["select"],
            json.dumps(features),
            json.dumps(spec),
            TRAINING,
            uuid.uuid4().hex,
            f"redshiftml-{stamp}-{(spec['model_type'] or 'automl').lower()}",
        ]
    )


def _drop_functions(row, execute) -> None:
    n = len(json.loads(row["features"]))
    types = ", ".join(["float8"] * n)
    for suffix in ("", "_probabilities"):
        fn = f"{_q(row['schema_name'])}.{_q(row['function_name'] + suffix)}"
        execute(f"DROP FUNCTION IF EXISTS {fn}({types})")


def drop_model(plpy, qualified: str, if_exists: bool) -> None:
    """DROP MODEL [IF EXISTS]: remove the model and its prediction functions."""
    schema, name = _resolve(plpy, qualified)
    row = _find(plpy, schema, name)
    if row is None:
        if if_exists:
            plpy.notice(f'Model "{schema}.{name}" does not exist, skipping')
            return
        plpy.error(f'Model "{schema}.{name}" does not exist')
    _drop_functions(row, plpy.execute)
    plan = plpy.prepare(
        "DELETE FROM pg_oblako.models WHERE schema_name = $1 AND model_name = $2",
        ["text", "text"],
    )
    plan.execute([schema, name])


def _fmt_time(ts) -> str:
    return str(ts or "")


def show_model(plpy, qualified: str) -> list[tuple[str, str]]:
    """SHOW MODEL <name>: the Key / Value report Redshift prints."""
    schema, name = _resolve(plpy, qualified)
    row = _find(plpy, schema, name)
    if row is None:
        plpy.error(f'Model "{schema}.{name}" does not exist')
    spec = json.loads(row["spec"])
    metrics = json.loads(row["metrics"] or "{}")
    features = json.loads(row["features"])
    out = [
        ("Model Name", name),
        ("Schema Name", schema),
        ("Owner", row["owner"]),
        ("Creation Time", _fmt_time(row["created_at"])),
        ("Model State", row["model_state"]),
    ]
    if row["model_state"] == FAILED:
        out.append(("Failure Reason", row["failure_reason"] or ""))
    if "metric" in metrics:
        out.append((metrics["metric"], f"{metrics['value']:.6f}"))
    if row["train_seconds"] is not None:
        out.append(("Training Job Time (seconds)", str(row["train_seconds"])))
    out += [
        ("Estimated Cost", "0.000000"),
        ("", ""),
        ("TRAINING DATA:", ""),
        ("Query", row["query"]),
        ("Target Column", row["target"].upper()),
        ("", ""),
        ("PARAMETERS:", ""),
        ("Model Type", (row["model_type"] or spec["model_type"] or "auto").lower()),
        ("Problem Type", row["problem_type"] or spec["problem_type"] or ""),
    ]
    if spec["objective"]:
        out.append(("Objective", spec["objective"]))
    out += [
        ("AutoML", "ON" if spec["auto"] else "OFF"),
        ("Training Job Name", row["training_job_name"]),
        ("Function Name", row["function_name"]),
        ("Function Parameters", " ".join(features)),
        ("Function Parameter Types", " ".join(["float8"] * len(features))),
        ("IAM Role", spec["iam_role"] or "default-aws-iam-role"),
        ("S3 Bucket", spec["settings"].get("s3_bucket", "")),
        ("Max Runtime", spec["settings"].get("max_runtime", "5400")),
    ]
    if metrics.get("candidates"):
        out += [("", ""), ("AUTOPILOT CANDIDATES:", "")]
        out += [
            (c["model_type"].lower(), f"{c['val_score']:.6f}")
            for c in metrics["candidates"]
        ]
    out += [("", ""), ("HYPERPARAMETERS:", "")]
    hp = dict(spec["hyperparameters"])
    if spec["objective"]:
        hp["objective"] = spec["objective"]
    out += [(k, v) for k, v in sorted(hp.items())]
    return out


def show_models(plpy) -> list[tuple[str, str]]:
    """SHOW MODEL ALL: every model in this database."""
    rows = plpy.execute(
        "SELECT schema_name, model_name FROM pg_oblako.models ORDER BY 1, 2"
    )
    return [(r["schema_name"], r["model_name"]) for r in rows]


# -----------------------------------------------------------------------------
# The training agent (root, outside PostgreSQL): TRAINING -> READY / FAILED
# -----------------------------------------------------------------------------
def _hyperparameters(spec: dict, problem_type: str, model_type: str) -> dict:
    hp = {k: str(v) for k, v in spec["hyperparameters"].items()}
    hp.update(problem_type=problem_type, model_type=model_type, autopilot="true")
    if spec["objective"]:
        hp["objective"] = spec["objective"]
    return hp


def _run_training(client, image: str, rows_csv: bytes, hp: dict, timeout: int) -> dict:
    """One training container on the /opt/ml contract; returns model.json."""
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        for path, data in (
            ("opt/ml/input/config/hyperparameters.json", json.dumps(hp).encode()),
            ("opt/ml/input/data/train/train.csv", rows_csv),
        ):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    container = client.containers.create(
        image, name=f"oblako-redshift-ml-{uuid.uuid4().hex[:10]}", detach=True
    )
    try:
        container.put_archive("/", payload.getvalue())
        container.start()
        try:
            code = container.wait(timeout=timeout).get("StatusCode", 1)
        except Exception as err:
            raise RuntimeError(
                f"training exceeded MAX_RUNTIME ({timeout}s) and was stopped"
            ) from err
        if code != 0:
            logs = container.logs().decode("utf-8", "replace")
            raise RuntimeError(f"training container exited {code}: {logs[-1500:]}")
        bits, _ = container.get_archive("/opt/ml/model/model.json")
        with tarfile.open(fileobj=io.BytesIO(b"".join(bits))) as tar:
            member = tar.extractfile(tar.getmembers()[0])
            if member is None:
                raise RuntimeError("the training container wrote no model.json")
            return json.loads(member.read())
    finally:
        try:
            container.remove(force=True)
        except Exception:  # best-effort cleanup
            pass


def _train_image() -> str:
    """Tag the training image by its build context, so a changed trainer rebuilds."""
    import hashlib

    digest = hashlib.sha256()
    for name in sorted(os.listdir(TRAIN_CONTEXT)):
        with open(os.path.join(TRAIN_CONTEXT, name), "rb") as fh:
            digest.update(name.encode() + fh.read())
    return f"{TRAIN_IMAGE}-{digest.hexdigest()[:12]}"


def _ensure_train_image(client) -> str:
    """Build the training image on first use (per build-context hash)."""
    tag = _train_image()
    try:
        client.images.get(tag)
    except Exception:  # absent -> build it from the baked context
        client.images.build(path=TRAIN_CONTEXT, tag=tag, rm=True)
    return tag


def train_one(conn, row: dict) -> None:
    """Train one queued model and publish its prediction function(s)."""
    import docker

    spec = json.loads(row["spec"])
    started = time.time()
    from psycopg.rows import tuple_row

    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(f"SELECT * FROM ({row['query']}) AS _oblako_ml")
        columns = [d.name for d in cur.description]
        data = cur.fetchall()
    ti = columns.index(row["target"])
    fi = [i for i, c in enumerate(columns) if c != row["target"]]

    def num(v):
        return "nan" if v is None else repr(float(v))

    y = []
    lines = []
    for r in data:
        if r[ti] is None:
            continue  # rows without a label can't train
        y.append(float(r[ti]))
        lines.append(",".join([num(r[i]) for i in fi] + [repr(float(r[ti]))]))
    csv_bytes = ("\n".join(lines) + "\n").encode()
    problem_type = spec["problem_type"] or detect_problem_type(y)

    client = docker.DockerClient(base_url=f"unix://{DOCKER_SOCKET}")
    image = _ensure_train_image(client)
    timeout = int(spec["settings"].get("max_runtime", 5400))
    candidates: list[dict[str, Any]] = []
    if spec["autopilot"]:
        best = None
        for model_type in MODEL_TYPES:
            trained = _run_training(
                client,
                image,
                csv_bytes,
                _hyperparameters(spec, problem_type, model_type),
                timeout,
            )
            score = trained.get("val_score", float("-inf"))
            candidates.append({"model_type": model_type, "val_score": score})
            if best is None or score > best[0]:
                best = (score, model_type, trained)
        if best is None:
            raise RuntimeError("AutoML trained no candidate model")
        _, model_type, model = best
    else:
        model_type = spec["model_type"]
        model = _run_training(
            client,
            image,
            csv_bytes,
            _hyperparameters(spec, problem_type, model_type),
            timeout,
        )
    classify = problem_type != "regression"
    metrics = {
        "metric": "validation:accuracy" if classify else "validation:r2",
        "value": model.get("val_score", 0.0),
    }
    if candidates:
        metrics["candidates"] = sorted(candidates, key=lambda c: -c["val_score"])

    n = len(fi)
    with conn.cursor() as cur:
        super_type = cur.execute(
            "SELECT to_regtype('super') IS NOT NULL AS ok"
        ).fetchone()["ok"]
        cur.execute(
            "UPDATE pg_oblako.models SET model = %s, model_type = %s, problem_type = %s, "
            "metrics = %s, train_seconds = %s WHERE version = %s",
            (
                json.dumps(model),
                model_type,
                problem_type,
                json.dumps(metrics),
                int(time.time() - started),
                row["version"],
            ),
        )
        fns = [(False, "float8")]
        if spec["auto"] and classify:  # Autopilot classification: label probabilities
            fns.append((True, "super" if super_type else "jsonb"))
        for probabilities, returns in fns:
            cur.execute(
                udf_sql(
                    row["schema_name"],
                    row["function_name"],
                    row["version"],
                    n,
                    problem_type,
                    model_type,
                    probabilities,
                    returns,
                )
            )
            name = row["function_name"] + ("_probabilities" if probabilities else "")
            cur.execute(
                f"ALTER FUNCTION {_q(row['schema_name'])}.{_q(name)}"
                f"({', '.join(['float8'] * n)}) OWNER TO {_q(row['owner'])}"
            )
        cur.execute(
            "UPDATE pg_oblako.models SET model_state = %s, trained_at = now() "
            "WHERE version = %s",
            (READY, row["version"]),
        )
    conn.commit()


def _connect(dbname: str):
    import psycopg
    from psycopg.rows import DictRow, dict_row

    # Connection[DictRow] names the row type that row_factory=dict_row gives
    return psycopg.Connection[DictRow].connect(
        host=os.environ.get("OBLAKO_PG_SOCKET_DIR", "/var/run/postgresql"),
        port=int(os.environ.get("OBLAKO_PG_PORT", "5433")),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        dbname=dbname,
        row_factory=dict_row,
    )


def _job(dbname: str, row: dict) -> None:
    try:
        with _connect(dbname) as conn:
            try:
                train_one(conn, row)
            except Exception as err:  # surfaces in model_state
                conn.rollback()
                conn.execute(
                    "UPDATE pg_oblako.models SET model_state = %s, "
                    "failure_reason = %s WHERE version = %s",
                    (FAILED, str(err).strip()[:2000], row["version"]),
                )
                conn.commit()
    except Exception as err:  # keep the agent alive
        print(f"redshift-ml: {dbname}.{row.get('model_name')}: {err}", flush=True)


def _claim(dbname: str) -> list[dict]:
    with _connect(dbname) as conn:
        if (
            conn.execute("SELECT to_regclass('pg_oblako.models')").fetchone()[
                "to_regclass"
            ]
            is None
        ):
            return []
        rows = conn.execute(
            "UPDATE pg_oblako.models SET claimed_at = now() "
            "WHERE model_state = %s AND claimed_at IS NULL RETURNING *",
            (TRAINING,),
        ).fetchall()
        conn.commit()
        return rows


INSTALL_SQL = "/docker-entrypoint-initdb.d/09_redshift_ml.sql"


def _install(dbname: str) -> None:
    """(Re)install the Redshift ML catalog and functions: idempotent.

    initdb.d only runs on a fresh data volume, so an engine upgraded in place
    would otherwise never get Redshift ML. A restart also re-queues any model a
    previous agent left mid-training.
    """
    with open(INSTALL_SQL) as fh:
        ddl = fh.read()
    with _connect(dbname) as conn:
        conn.execute(ddl)
        conn.execute(
            "UPDATE pg_oblako.models SET claimed_at = NULL WHERE model_state = %s",
            (TRAINING,),
        )


def agent(interval: float = 2.0) -> None:
    """Poll every database for TRAINING models and train them in the background."""
    print("redshift-ml agent: watching for CREATE MODEL", flush=True)
    installed: set[str] = set()
    while True:
        try:
            with _connect(os.environ.get("POSTGRES_DB", "postgres")) as conn:
                every = conn.execute(
                    "SELECT datname, datistemplate FROM pg_database WHERE datallowconn"
                ).fetchall()
            for r in every:
                if r["datname"] not in installed:
                    _install(r["datname"])  # template1 too, for databases made later
                    installed.add(r["datname"])
            dbs = [r["datname"] for r in every if not r["datistemplate"]]
            for db in dbs:
                for row in _claim(db):
                    threading.Thread(target=_job, args=(db, row), daemon=True).start()
        except Exception as err:  # the database may be restarting
            print(f"redshift-ml agent: {err}", flush=True)
        time.sleep(interval)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "agent":
    agent()
