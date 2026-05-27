"""Integration tests for Redshift ML (CREATE MODEL) across model types.

Requires: pip install 'oblako[sagemaker]', Docker, and pgredshift on 5439.
Each model is trained in a real SageMaker local container, so this is slow and
skipped unless the pieces are present.
"""

import psycopg2
import pytest

pytest.importorskip("sagemaker")
try:
    from sagemaker.local import LocalSession  # noqa: F401
except Exception:  # pragma: no cover
    pytest.skip("sagemaker local mode unavailable", allow_module_level=True)

from oblako.redshift_data.executor import RedshiftDataExecutor

PG = dict(
    host="localhost", port=5439, user="oblako", password="oblako", database="oblako"
)
MODEL_TYPES = ["LINEAR_LEARNER", "MLP", "XGBOOST"]


def _decode(record):
    return [next(iter(f.values())) for f in record]


@pytest.fixture(scope="module")
def executor():
    try:
        import docker

        docker.from_env().ping()
        psycopg2.connect(
            host="localhost",
            port=5439,
            user="oblako",
            password="oblako",
            dbname="oblako",
        ).close()
    except Exception:
        pytest.skip("Docker or pgredshift not available")

    conn = psycopg2.connect(
        host="localhost", port=5439, user="oblako", password="oblako", dbname="oblako"
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS ml_homes")
    cur.execute("CREATE TABLE ml_homes (sqft FLOAT, beds FLOAT, price FLOAT)")
    cur.executemany(
        "INSERT INTO ml_homes VALUES (%s,%s,%s)",
        [
            (s, b, 100.0 * s + 5000.0 * b)
            for s in (800, 1000, 1200, 1500, 2000, 2500)
            for b in (1, 2, 3, 4)
        ],
    )
    cur.execute("DROP TABLE IF EXISTS ml_apps")
    cur.execute("CREATE TABLE ml_apps (score FLOAT, income FLOAT, approved FLOAT)")
    cur.executemany(
        "INSERT INTO ml_apps VALUES (%s,%s,%s)",
        [
            (sc, inc, 1.0 if (sc > 660 and inc > 40) else 0.0)
            for sc in range(600, 760, 10)
            for inc in (20, 35, 50, 80)
        ],
    )
    cur.execute("DROP TABLE IF EXISTS ml_iris")
    cur.execute("CREATE TABLE ml_iris (x1 FLOAT, x2 FLOAT, cls FLOAT)")
    cur.executemany(
        "INSERT INTO ml_iris VALUES (%s,%s,%s)",
        [
            (i * 0.5, x2, 0.0 if i * 0.5 < 3.5 else (1.0 if i * 0.5 < 7.0 else 2.0))
            for i in range(2, 20)
            for x2 in (1.0, 2.0, 3.0)
        ],
    )
    cur.close()
    conn.close()
    return RedshiftDataExecutor(**PG)


def _create(executor, sql):
    desc = executor.describe(executor.execute(sql))
    assert desc["Status"] == "FINISHED", desc.get("Error")


def _query(executor, sql):
    return [_decode(r) for r in executor.result(executor.execute(sql))["Records"]]


@pytest.mark.parametrize("model_type", MODEL_TYPES)
def test_regression(executor, model_type):
    name = f"reg_{model_type.lower()}"
    fn = f"predict_{name}"
    _create(
        executor,
        f"CREATE MODEL {name} FROM (SELECT sqft, beds, price FROM ml_homes) "
        f"TARGET price FUNCTION {fn} MODEL_TYPE {model_type}",
    )
    rows = _query(executor, f"SELECT {fn}(sqft, beds) AS pred, price FROM ml_homes")
    errors = [abs(pred - actual) / actual for pred, actual in rows]
    assert max(errors) < 0.1, f"{model_type} max rel error {max(errors):.3f}"


@pytest.mark.parametrize("model_type", MODEL_TYPES)
def test_classification(executor, model_type):
    name = f"clf_{model_type.lower()}"
    fn = f"predict_{name}"
    _create(
        executor,
        f"CREATE MODEL {name} FROM (SELECT score, income, approved FROM ml_apps) "
        f"TARGET approved FUNCTION {fn} MODEL_TYPE {model_type}",
    )
    yes = _query(executor, f"SELECT {fn}(720, 80)")[0][0]
    no = _query(executor, f"SELECT {fn}(610, 20)")[0][0]
    assert yes == 1.0 and no == 0.0


@pytest.mark.parametrize("model_type", MODEL_TYPES)
def test_multiclass(executor, model_type):
    name = f"mc_{model_type.lower()}"
    fn = f"predict_{name}"
    _create(
        executor,
        f"CREATE MODEL {name} FROM (SELECT x1, x2, cls FROM ml_iris) "
        f"TARGET cls FUNCTION {fn} MODEL_TYPE {model_type} "
        f"PROBLEM_TYPE multiclass_classification",
    )
    preds = {
        x1: _query(executor, f"SELECT {fn}({x1}, 2.0)")[0][0] for x1 in (1.0, 5.0, 9.0)
    }
    assert preds == {1.0: 0.0, 5.0: 1.0, 9.0: 2.0}, f"{model_type}: {preds}"


def test_autopilot_selects_and_predicts(executor):
    fn = "predict_autopilot"
    # no MODEL_TYPE -> Autopilot; classes {0,1,2} -> auto-detected multiclass
    stmt = executor.execute(
        "CREATE MODEL autopilot_iris FROM (SELECT x1, x2, cls FROM ml_iris) "
        f"TARGET cls FUNCTION {fn}"
    )
    desc = executor.describe(stmt)
    assert desc["Status"] == "FINISHED", desc.get("Error")
    summary = desc["ModelSummary"]
    assert summary["problem_type"] == "multiclass_classification"
    assert {r["model_type"] for r in summary["autopilot"]} == set(MODEL_TYPES)
    assert summary["selected"] in MODEL_TYPES
    scores = [r["val_score"] for r in summary["autopilot"]]
    assert scores == sorted(scores, reverse=True)  # leaderboard sorted best-first
    preds = {
        x1: _query(executor, f"SELECT {fn}({x1}, 2.0)")[0][0] for x1 in (1.0, 5.0, 9.0)
    }
    assert preds == {1.0: 0.0, 5.0: 1.0, 9.0: 2.0}
