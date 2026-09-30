"""Pure-unit tests for Redshift ML (no SageMaker / Docker / the Redshift engine).

Covers the CREATE MODEL parser and its AUTO OFF validation, the wire-proxy
rewrite of CREATE/SHOW/DROP MODEL, problem-type detection, and the in-DB
inference math of the generated plpython3u prediction functions.
"""

import ast
import json
import math
import textwrap

import pytest

from oblako.engines.redshift_ml import (
    MLError,
    detect_problem_type,
    linear_body,
    mlp_body,
    parse_create_model,
    rewrite_ml,
    udf_sql,
    xgb_body,
)

AUTO_OFF_XGB = """
create model sandbox.pl_demo
from (
    select a::double precision as f_a, b::double precision as f_b, target
    from sandbox.tape where sample = 'train'
)
target target
function pl_predict
iam_role 'arn:aws:iam::000000000000:role/redshift-ml'
auto off
model_type xgboost
objective 'binary:logistic'
preprocessors 'none'
hyperparameters default except (
    num_round '150', max_depth '5', eta '0.1', eval_metric 'auc'
)
settings (s3_bucket 'ml-bucket', max_runtime 1800);
"""


def _predict(body):
    """Compile a function body into p(m, x), the way plpython3u runs it."""
    src = "def _p(m, x):\n" + textwrap.indent("import json, math\n" + body, "    ")
    ns: dict = {}
    exec(src, ns)  # exercise the exact code generated for the UDF
    return ns["_p"]


# -----------------------------------------------------------------------------
# Parser
# -----------------------------------------------------------------------------
def test_parses_a_schema_qualified_auto_off_xgboost_model():
    spec = parse_create_model(AUTO_OFF_XGB)
    assert (spec["schema"], spec["name"]) == ("sandbox", "pl_demo")
    assert spec["function"] == "pl_predict" and spec["function_schema"] is None
    assert spec["target"] == "target"
    assert spec["select"].startswith("select a::double precision as f_a")
    assert spec["auto"] is False and spec["model_type"] == "XGBOOST"
    assert spec["objective"] == "binary:logistic"
    assert spec["problem_type"] == "binary_classification"  # from the objective
    assert spec["hyperparameters"] == {
        "num_round": "150",
        "max_depth": "5",
        "eta": "0.1",
        "eval_metric": "auc",
    }
    assert spec["settings"] == {"s3_bucket": "ml-bucket", "max_runtime": "1800"}
    assert spec["iam_role"] == "arn:aws:iam::000000000000:role/redshift-ml"


def test_from_a_table_and_clauses_in_any_order():
    spec = parse_create_model(
        "CREATE MODEL churn FROM customer_activity PROBLEM_TYPE "
        "BINARY_CLASSIFICATION TARGET churn FUNCTION churn_predict "
        "IAM_ROLE default AUTO ON SETTINGS (S3_BUCKET 'b')"
    )
    assert spec["select"] == "SELECT * FROM customer_activity"
    assert spec["problem_type"] == "binary_classification"
    assert spec["auto"] is True and spec["autopilot"] is True


def test_model_type_pins_the_algorithm_without_auto_off():
    single = parse_create_model(
        "CREATE MODEL m FROM (SELECT a,b,t FROM x) TARGET t FUNCTION f MODEL_TYPE MLP"
    )
    assert single["model_type"] == "MLP" and single["autopilot"] is False
    assert single["auto"] is True


def test_quoted_names_keep_case_and_unquoted_fold():
    spec = parse_create_model(
        'CREATE MODEL "Sales"."Churn" FROM (SELECT a, y FROM t) TARGET Y FUNCTION F'
    )
    assert (spec["schema"], spec["name"]) == ("Sales", "Churn")
    assert spec["target"] == "y" and spec["function"] == "f"


@pytest.mark.parametrize(
    ("clauses", "message"),
    [
        ("AUTO OFF", "AUTO OFF requires MODEL_TYPE"),
        ("AUTO OFF MODEL_TYPE MLP", "AUTO OFF supports only MODEL_TYPE XGBOOST"),
        (
            "AUTO OFF MODEL_TYPE XGBOOST PREPROCESSORS 'none' HYPERPARAMETERS DEFAULT",
            "requires OBJECTIVE",
        ),
        (
            "AUTO OFF MODEL_TYPE XGBOOST OBJECTIVE 'reg:squarederror' "
            "PREPROCESSORS 'none'",
            "AUTO OFF requires HYPERPARAMETERS",
        ),
        (
            "AUTO OFF MODEL_TYPE XGBOOST OBJECTIVE 'reg:squarederror' "
            "HYPERPARAMETERS DEFAULT",
            "AUTO OFF requires PREPROCESSORS",
        ),
    ],
)
def test_auto_off_requires_every_clause(clauses, message):
    with pytest.raises(MLError, match=message):
        parse_create_model(
            f"CREATE MODEL m FROM (SELECT a, t FROM x) TARGET t FUNCTION f {clauses}"
        )


def test_unsupported_objective_and_preprocessors_are_rejected():
    base = "CREATE MODEL m FROM (SELECT a, t FROM x) TARGET t FUNCTION f "
    with pytest.raises(MLError, match="unsupported OBJECTIVE"):
        parse_create_model(base + "MODEL_TYPE XGBOOST OBJECTIVE 'rank:pairwise'")
    with pytest.raises(MLError, match="PREPROCESSORS 'none'"):
        parse_create_model(base + 'PREPROCESSORS \'[{"ColumnSet": ["a"]}]\'')


def test_detect_problem_type():
    assert detect_problem_type([0.0, 1.0, 1.0, 0.0]) == "binary_classification"
    assert detect_problem_type([0.0, 1.0, 2.0, 1.0, 2.0]) == "multiclass_classification"
    assert detect_problem_type([1.5, 2.7, 9.1]) == "regression"


# -----------------------------------------------------------------------------
# Wire-proxy rewrite
# -----------------------------------------------------------------------------
def test_rewrite_wraps_create_model_in_a_dollar_quoted_call():
    out = rewrite_ml(AUTO_OFF_XGB)
    assert out.strip().startswith(
        "SELECT oblako_ml_create_model($oblako_ml$create model"
    )
    assert out.rstrip().endswith("$oblako_ml$);")
    assert "sample = 'train'" in out  # quotes survive inside the dollar quote


def test_rewrite_show_and_drop():
    assert rewrite_ml("SHOW MODEL ALL;") == "SELECT * FROM oblako_ml_show_models();"
    assert (
        rewrite_ml("show model sandbox.pl_demo")
        == "SELECT * FROM oblako_ml_show_model('sandbox.pl_demo')"
    )
    assert (
        rewrite_ml("drop model if exists sandbox.pl_demo;")
        == "SELECT oblako_ml_drop_model('sandbox.pl_demo', true);"
    )
    assert rewrite_ml("DROP MODEL m") == "SELECT oblako_ml_drop_model('m', false)"


def test_rewrite_handles_a_batch_and_leaves_other_sql_alone():
    batch = "drop model if exists m; select 'drop model x' as s; show model m;"
    assert rewrite_ml(batch) == (
        "SELECT oblako_ml_drop_model('m', true); select 'drop model x' as s; "
        "SELECT * FROM oblako_ml_show_model('m');"
    )
    plain = "SELECT model_name FROM svv_ml_model_info"
    assert rewrite_ml(plain) is plain


# -----------------------------------------------------------------------------
# Generated prediction functions
# -----------------------------------------------------------------------------
def test_every_generated_function_compiles():
    for mt in ("LINEAR_LEARNER", "MLP", "XGBOOST"):
        for pt in ("regression", "binary_classification", "multiclass_classification"):
            for probs in (False, True):
                if probs and pt == "regression":
                    continue
                sql = udf_sql("s", "f", "v1", 2, pt, mt, probs, "super")
                body = sql.split("$oblako_udf$", 2)[1]
                ast.parse(body)
                assert '"s"."f' in sql and "(float8, float8)" in sql


def test_linear_multiclass_argmax_and_probabilities():
    m = {
        "classes": [10.0, 20.0, 30.0],
        "weights": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        "intercepts": [0, 0, 0],
    }
    p = _predict(linear_body("multiclass_classification"))
    assert p(m, [5, 1, 1]) == 10.0 and p(m, [1, 1, 5]) == 30.0
    probs = _predict(
        "def _probs(ps, labels):\n"
        "    return dict(probabilities=ps, labels=labels)\n"
        + linear_body("multiclass_classification", probabilities=True)
    )(m, [5, 1, 1])
    assert probs["labels"] == [10.0, 20.0, 30.0]
    assert math.isclose(sum(probs["probabilities"]), 1.0)
    assert probs["probabilities"][0] == max(probs["probabilities"])


def test_xgb_binary_label_probability_and_missing_branch():
    tree = {
        "0": {"f": 0, "c": 0.5, "y": 1, "n": 2, "m": 1},
        "1": {"leaf": -2.0},
        "2": {"leaf": 2.0},
    }
    m = {"trees": [tree], "base_score": 0.5}
    label = _predict(xgb_body("binary_classification"))
    assert label(m, [0.9]) == 1.0 and label(m, [0.1]) == 0.0
    assert label(m, [float("nan")]) == 0.0  # NULL takes the missing branch (-2)
    probs = _predict(
        "def _probs(ps, labels):\n    return ps\n"
        + xgb_body("binary_classification", probabilities=True)
    )(m, [0.9])
    assert math.isclose(probs[1], 1 / (1 + math.exp(-2.0)))


def test_xgb_reg_logistic_returns_a_probability():
    m = {"trees": [{"0": {"leaf": 1.0}}], "base_score": 0.5}
    p = _predict(xgb_body("regression"))
    assert p(dict(m), [0.0]) == 1.5  # squared error: base + leaves
    assert math.isclose(
        p(dict(m, objective="reg:logistic"), [0.0]), 1 / (1 + math.exp(-1.0))
    )


def test_xgb_multiclass_argmax():
    p = _predict(xgb_body("multiclass_classification"))

    def leaf(v):  # a tiny single-leaf tree
        return {"0": {"leaf": v}}

    m = {
        "classes": [7.0, 8.0, 9.0],
        "num_class": 3,
        "trees": [leaf(0.1), leaf(0.9), leaf(0.2)],
    }  # class index 1 wins
    assert p(m, [0.0, 0.0]) == 8.0


def test_mlp_multiclass_argmax():
    p = _predict(mlp_body("multiclass_classification"))
    m = {
        "scaler": {"mean": [0, 0, 0], "std": [1, 1, 1]},
        "layers": [{"W": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "b": [0, 0, 0]}],
        "hidden_activation": "relu",
        "out_activation": "softmax",
        "classes": [100.0, 200.0, 300.0],
    }
    assert p(m, [5, 1, 1]) == 100.0
    assert p(m, [1, 1, 5]) == 300.0


def test_binary_and_regression_linear():
    bp = _predict(linear_body("binary_classification"))
    m = {"weights": [10.0], "intercept": -5.0}
    assert bp(m, [1.0]) == 1.0 and bp(m, [0.0]) == 0.0
    rp = _predict(linear_body("regression"))
    assert rp({"weights": [2.0], "intercept": 1.0}, [3.0]) == 7.0


def test_probabilities_payload_shape():
    body = udf_sql("s", "f", "v", 1, "binary_classification", "LINEAR_LEARNER", True)
    src = body.split("$oblako_udf$", 2)[1]
    start = src.index("def _probs")
    ns: dict = {"json": json}
    exec(src[start : src.index("z = ")], ns)  # the generated helper
    assert json.loads(ns["_probs"]([0.25, 0.75], [0.0, 1.0])) == {
        "probabilities": [0.25, 0.75],
        "labels": ["0", "1"],
    }
