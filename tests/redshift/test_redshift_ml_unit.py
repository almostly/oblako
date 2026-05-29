"""Pure-unit tests for Redshift ML (no SageMaker / Docker / pgredshift).

Covers the CREATE MODEL parser, problem-type auto-detection, and the in-DB
inference math of the generated plpython3u UDF bodies — including multiclass.
"""

import textwrap

from oblako.engines.redshift_ml import (
    _detect_problem_type,
    _linear_body,
    _mlp_body,
    _udf_sql,
    _xgb_body,
    parse_create_model,
)


def _predict(body):
    """Compile a UDF body into a callable p(m, x), as plpython3u would run it."""
    src = "def _p(m, x):\n" + textwrap.indent("import math\n" + body, "    ")
    ns: dict = {}
    exec(src, ns)  # noqa: S102 - exercising the exact code generated for the UDF
    return ns["_p"]


def test_parser_model_type_vs_autopilot():
    single = parse_create_model(
        "CREATE MODEL m FROM (SELECT a,b,t FROM x) "
        "TARGET t FUNCTION f MODEL_TYPE XGBOOST"
    )
    assert single["model_type"] == "XGBOOST" and single["autopilot"] is False
    auto = parse_create_model(
        "CREATE MODEL m FROM (SELECT a,b,t FROM x) TARGET t FUNCTION f"
    )
    assert auto["model_type"] is None and auto["autopilot"] is True
    auto_on = parse_create_model(
        "CREATE MODEL m FROM (SELECT a,b,t FROM x) TARGET t FUNCTION f AUTO ON"
    )
    assert auto_on["autopilot"] is True


def test_parser_auto_off_requires_model_type():
    import pytest

    with pytest.raises(ValueError, match="AUTO OFF requires MODEL_TYPE"):
        parse_create_model(
            "CREATE MODEL m FROM (SELECT a,b,t FROM x) TARGET t FUNCTION f AUTO OFF"
        )


def test_parser_multiclass_problem_and_objective():
    p = parse_create_model(
        "CREATE MODEL m FROM (SELECT a,b,t FROM x) TARGET t FUNCTION f "
        "MODEL_TYPE MLP PROBLEM_TYPE multiclass_classification"
    )
    assert p["problem_type"] == "multiclass_classification"
    o = parse_create_model(
        "CREATE MODEL m FROM (SELECT a,b,t FROM x) TARGET t FUNCTION f "
        "MODEL_TYPE XGBOOST OBJECTIVE 'multi:softprob'"
    )
    assert o["problem_type"] == "multiclass_classification"


def test_detect_problem_type():
    assert _detect_problem_type([0.0, 1.0, 1.0, 0.0]) == "binary_classification"
    assert (
        _detect_problem_type([0.0, 1.0, 2.0, 1.0, 2.0]) == "multiclass_classification"
    )
    assert _detect_problem_type([1.5, 2.7, 9.1]) == "regression"


def test_all_udf_bodies_compile():
    import ast

    for mt in ("LINEAR_LEARNER", "MLP", "XGBOOST"):
        for pt in ("regression", "binary_classification", "multiclass_classification"):
            sql = _udf_sql("m", "f", ["a", "b"], pt, mt)
            ast.parse(sql.split("AS $$", 1)[1].rsplit("$$", 1)[0])


def test_linear_multiclass_argmax():
    p = _predict(_linear_body("multiclass_classification"))
    m = {
        "classes": [10.0, 20.0, 30.0],
        "weights": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        "intercepts": [0, 0, 0],
    }
    assert p(m, [5, 1, 1]) == 10.0
    assert p(m, [1, 5, 1]) == 20.0
    assert p(m, [1, 1, 5]) == 30.0


def test_xgb_multiclass_argmax():
    p = _predict(_xgb_body("multiclass_classification"))
    leaf = lambda v: {"0": {"leaf": v}}  # noqa: E731 - tiny single-leaf tree
    m = {
        "classes": [7.0, 8.0, 9.0],
        "num_class": 3,
        "trees": [leaf(0.1), leaf(0.9), leaf(0.2)],
    }  # class index 1 wins
    assert p(m, [0.0, 0.0]) == 8.0


def test_mlp_multiclass_argmax():
    p = _predict(_mlp_body("multiclass_classification"))
    m = {
        "scaler": {"mean": [0, 0, 0], "std": [1, 1, 1]},
        "layers": [{"W": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "b": [0, 0, 0]}],
        "hidden_activation": "relu",
        "out_activation": "softmax",
        "classes": [100.0, 200.0, 300.0],
    }
    assert p(m, [5, 1, 1]) == 100.0
    assert p(m, [1, 1, 5]) == 300.0


def test_binary_and_regression_still_work():
    bp = _predict(_linear_body("binary_classification"))
    m = {"weights": [10.0], "intercept": -5.0}
    assert bp(m, [1.0]) == 1.0 and bp(m, [0.0]) == 0.0
    rp = _predict(_linear_body("regression"))
    assert rp({"weights": [2.0], "intercept": 1.0}, [3.0]) == 7.0
