#!/usr/bin/env python3
"""Redshift ML training entry point (SageMaker 'bring your own container').

Trains the model type requested by hyperparameters and exports it to plain JSON
that a pure-Python plpython3u UDF can evaluate in pgredshift (which has no
numpy/sklearn/xgboost). Supports Redshift ML's supervised types —
  LINEAR_LEARNER -> linear / logistic regression (scikit-learn)
  MLP            -> multilayer perceptron        (scikit-learn, with scaling)
  XGBOOST        -> gradient-boosted trees        (xgboost)
for regression, binary, and multiclass classification. When `autopilot` is set,
also reports a holdout validation score so the caller can pick the best type.
"""

import csv
import glob
import json
import os

INPUT = "/opt/ml/input/data/train"
MODEL = "/opt/ml/model"
CONFIG = "/opt/ml/input/config/hyperparameters.json"


def _load():
    X, y = [], []
    for path in sorted(glob.glob(os.path.join(INPUT, "*.csv"))):
        with open(path) as fh:
            for row in csv.reader(fh):
                if not row:
                    continue
                *features, target = row
                X.append([float(v) for v in features])
                y.append(float(target))
    if not X:
        raise SystemExit("no training data found in /opt/ml/input/data/train")
    return X, y


# Per-type training -> plain-JSON export
def train_linear(X, y, classify, multiclass):
    """Train a linear/logistic regression model and return it as a plain-JSON-serializable dict."""
    from sklearn.linear_model import LinearRegression, LogisticRegression

    if multiclass:
        clf = LogisticRegression(max_iter=1000).fit(X, y)
        return {
            "multiclass": True,
            "classes": [float(c) for c in clf.classes_],
            "weights": clf.coef_.tolist(),
            "intercepts": clf.intercept_.tolist(),
        }
    if classify:
        clf = LogisticRegression(max_iter=1000).fit(X, y)
        return {"weights": clf.coef_[0].tolist(), "intercept": float(clf.intercept_[0])}
    reg = LinearRegression().fit(X, y)
    return {"weights": reg.coef_.tolist(), "intercept": float(reg.intercept_)}


def train_mlp(X, y, classify, multiclass):
    """Train an MLP model with StandardScaler and return it as a plain-JSON-serializable dict."""
    import statistics

    from sklearn.neural_network import MLPClassifier, MLPRegressor
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler().fit(X)
    Xs = scaler.transform(X).tolist()
    export = {
        "scaler": {"mean": scaler.mean_.tolist(), "std": scaler.scale_.tolist()},
        "y_mean": 0.0,
        "y_std": 1.0,
    }
    # lbfgs fits small datasets well; scale the target for regression so the net
    # isn't asked to output huge magnitudes.
    if classify:
        net = MLPClassifier(
            hidden_layer_sizes=(16, 8), solver="lbfgs", max_iter=2000, random_state=0
        ).fit(Xs, y)
        if multiclass:
            export["multiclass"] = True
            export["classes"] = [float(c) for c in net.classes_]
    else:
        y_mean = statistics.fmean(y)
        y_std = statistics.pstdev(y) or 1.0
        ys = [(v - y_mean) / y_std for v in y]
        net = MLPRegressor(
            hidden_layer_sizes=(16, 8), solver="lbfgs", max_iter=2000, random_state=0
        ).fit(Xs, ys)
        export["y_mean"] = y_mean
        export["y_std"] = y_std
    export["layers"] = [
        {"W": W.tolist(), "b": b.tolist()} for W, b in zip(net.coefs_, net.intercepts_)
    ]
    export["hidden_activation"] = net.activation
    # For multiclass the out activation is softmax; the UDF takes the argmax of the
    # final layer, and argmax(softmax(z)) == argmax(z), so it leaves logits as-is.
    export["out_activation"] = net.out_activation_
    return export


def _flatten_tree(node, out):
    nid = str(node["nodeid"])
    if "leaf" in node:
        out[nid] = {"leaf": node["leaf"]}
    else:
        out[nid] = {
            "f": int(node["split"][1:]),  # "f3" -> 3
            "c": node["split_condition"],
            "y": node["yes"],
            "n": node["no"],
        }
        for child in node["children"]:
            _flatten_tree(child, out)
    return out


def train_xgboost(X, y, classify, multiclass, hp):
    """Train an XGBoost model and return it as a plain-JSON-serializable dict."""
    import xgboost as xgb

    num_round = int(hp.get("num_round", 100))
    max_depth = int(hp.get("max_depth", 6))
    common = dict(n_estimators=num_round, max_depth=max_depth)
    if multiclass:
        classes = sorted(set(y))
        yi = [classes.index(v) for v in y]  # xgboost needs labels in [0, num_class)
        model = xgb.XGBClassifier(
            objective="multi:softprob", num_class=len(classes), **common
        ).fit(X, yi)
        dumps = model.get_booster().get_dump(dump_format="json")
        trees = [_flatten_tree(json.loads(d), {}) for d in dumps]
        return {
            "multiclass": True,
            "classes": [float(c) for c in classes],
            "num_class": len(classes),
            "trees": trees,
        }
    if classify:
        model = xgb.XGBClassifier(objective="binary:logistic", base_score=0.5, **common)
    else:
        model = xgb.XGBRegressor(objective="reg:squarederror", base_score=0.5, **common)
    model.fit(X, y)
    dumps = model.get_booster().get_dump(dump_format="json")
    trees = [_flatten_tree(json.loads(d), {}) for d in dumps]
    return {"trees": trees, "base_score": 0.5}


# Autopilot: holdout validation score so the caller can pick the best type
def _fit_scorer(model_type, X, y, classify, multiclass, hp):
    """Fit and return an estimator with a uniform .predict interface (labels / values)."""
    if model_type == "LINEAR_LEARNER":
        from sklearn.linear_model import LinearRegression, LogisticRegression

        return (
            LogisticRegression(max_iter=1000) if classify else LinearRegression()
        ).fit(X, y)
    if model_type == "MLP":
        from sklearn.neural_network import MLPClassifier, MLPRegressor
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        mlp_kw = dict(
            hidden_layer_sizes=(16, 8), solver="lbfgs", max_iter=2000, random_state=0
        )
        if classify:
            return make_pipeline(StandardScaler(), MLPClassifier(**mlp_kw)).fit(X, y)
        from sklearn.compose import TransformedTargetRegressor

        reg = make_pipeline(StandardScaler(), MLPRegressor(**mlp_kw))
        return TransformedTargetRegressor(
            regressor=reg, transformer=StandardScaler()
        ).fit(X, y)

    # XGBOOST
    import xgboost as xgb

    nr, md = int(hp.get("num_round", 100)), int(hp.get("max_depth", 6))
    if multiclass:
        classes = sorted(set(y))
        clf = xgb.XGBClassifier(
            objective="multi:softprob",
            num_class=len(classes),
            n_estimators=nr,
            max_depth=md,
        ).fit(X, [classes.index(v) for v in y])

        class _Wrapped:
            def predict(self, Xt):
                return [classes[int(i)] for i in clf.predict(Xt)]

        return _Wrapped()
    if classify:
        return xgb.XGBClassifier(
            objective="binary:logistic", base_score=0.5, n_estimators=nr, max_depth=md
        ).fit(X, y)
    return xgb.XGBRegressor(
        objective="reg:squarederror", base_score=0.5, n_estimators=nr, max_depth=md
    ).fit(X, y)


def _val_score(model_type, X, y, classify, multiclass, hp):
    """Accuracy (classification) or R^2 (regression) on a holdout split."""
    from sklearn.model_selection import train_test_split

    if len(X) >= 5:
        strat = y if classify else None
        try:
            Xtr, Xva, ytr, yva = train_test_split(
                X, y, test_size=0.25, random_state=0, stratify=strat
            )
        except ValueError:  # a class too rare to stratify
            Xtr, Xva, ytr, yva = train_test_split(X, y, test_size=0.25, random_state=0)
    else:
        Xtr, Xva, ytr, yva = X, X, y, y  # too small to hold out
    est = _fit_scorer(model_type, Xtr, ytr, classify, multiclass, hp)
    pred = list(est.predict(Xva))
    if classify:
        return sum(1.0 for p, t in zip(pred, yva) if float(p) == float(t)) / len(yva)
    import statistics

    mean = statistics.fmean(yva)
    ss_tot = sum((t - mean) ** 2 for t in yva) or 1e-9
    ss_res = sum((t - p) ** 2 for p, t in zip(pred, yva))
    return 1.0 - ss_res / ss_tot


def main():
    """Read hyperparameters, train the requested model type, and write model.json to /opt/ml/model."""
    hp = {}
    if os.path.exists(CONFIG):
        with open(CONFIG) as fh:
            hp = json.load(fh)
    model_type = hp.get("model_type", "LINEAR_LEARNER").upper()
    problem_type = hp.get("problem_type", "regression")
    classify = problem_type in ("binary_classification", "multiclass_classification")
    multiclass = problem_type == "multiclass_classification"

    X, y = _load()
    if model_type == "LINEAR_LEARNER":
        export = train_linear(X, y, classify, multiclass)
    elif model_type == "MLP":
        export = train_mlp(X, y, classify, multiclass)
    elif model_type == "XGBOOST":
        export = train_xgboost(X, y, classify, multiclass, hp)
    else:
        raise SystemExit(f"unsupported MODEL_TYPE: {model_type}")

    if hp.get("autopilot") == "true":
        export["val_score"] = _val_score(model_type, X, y, classify, multiclass, hp)

    export.update(
        model_type=model_type,
        problem_type=problem_type,
        n_features=len(X[0]),
        rows=len(y),
    )
    os.makedirs(MODEL, exist_ok=True)
    with open(os.path.join(MODEL, "model.json"), "w") as fh:
        json.dump(export, fh)
    score = f" val_score={export['val_score']:.4f}" if "val_score" in export else ""
    print(
        f"Trained {model_type}/{problem_type}: {len(y)} rows, {len(X[0])} features{score}"
    )


if __name__ == "__main__":
    main()
