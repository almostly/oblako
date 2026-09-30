"""Integration tests for Redshift ML: CREATE / SHOW / DROP MODEL over the wire.

Requires Docker and the Redshift engine on 5439, started with the Docker socket
mounted (``oblako up redshift`` / docker compose do). CREATE MODEL is asynchronous,
as on Amazon Redshift: it returns at once, then the engine's agent trains the
model in a container and publishes the prediction function. Tests poll
``svv_ml_model_info`` the way a user would (``wait_for_model``).
"""

import json
import random

import psycopg2
import pytest

from oblako.engines.redshift_data.executor import RedshiftDataExecutor
from oblako.engines.redshift_ml import wait_for_model

PG = dict(
    host="localhost", port=5439, user="oblako", password="oblako", dbname="oblako"
)
MODEL_TYPES = ["LINEAR_LEARNER", "MLP", "XGBOOST"]


def _connect():
    conn = psycopg2.connect(**PG)
    conn.autocommit = True
    return conn


@pytest.fixture(scope="module")
def cur():
    try:
        import docker

        docker.from_env().ping()
        conn = _connect()
    except Exception:
        pytest.skip("Docker or the Redshift engine not available")
    c = conn.cursor()
    c.execute("SELECT to_regprocedure('oblako_ml_create_model(text)') IS NOT NULL")
    if not c.fetchone()[0]:
        pytest.skip("redshift image without Redshift ML")

    c.execute("DROP TABLE IF EXISTS ml_homes")
    c.execute("CREATE TABLE ml_homes (sqft FLOAT, beds FLOAT, price FLOAT)")
    c.executemany(
        "INSERT INTO ml_homes VALUES (%s,%s,%s)",
        [
            (s, b, 100.0 * s + 5000.0 * b)
            for s in (800, 1000, 1200, 1500, 2000, 2500)
            for b in (1, 2, 3, 4)
        ],
    )
    c.execute("DROP TABLE IF EXISTS ml_apps")
    c.execute("CREATE TABLE ml_apps (score FLOAT, income FLOAT, approved FLOAT)")
    c.executemany(
        "INSERT INTO ml_apps VALUES (%s,%s,%s)",
        [
            (sc, inc, 1.0 if (sc > 660 and inc > 40) else 0.0)
            for sc in range(600, 760, 10)
            for inc in (20, 35, 50, 80)
        ],
    )
    c.execute("DROP TABLE IF EXISTS ml_iris")
    c.execute("CREATE TABLE ml_iris (x1 FLOAT, x2 FLOAT, cls FLOAT)")
    c.executemany(
        "INSERT INTO ml_iris VALUES (%s,%s,%s)",
        [
            (i * 0.5, x2, 0.0 if i * 0.5 < 3.5 else (1.0 if i * 0.5 < 7.0 else 2.0))
            for i in range(2, 20)
            for x2 in (1.0, 2.0, 3.0)
        ],
    )
    # a credit tape stored as varchar (a pandas dump), with a train/test split
    c.execute("CREATE SCHEMA IF NOT EXISTS ml_sandbox")
    c.execute("DROP TABLE IF EXISTS ml_sandbox.tape")
    c.execute(
        "CREATE TABLE ml_sandbox.tape (f1 varchar, f2 varchar, target varchar, "
        "sample varchar)"
    )
    rng = random.Random(0)
    rows = []
    for i in range(1200):
        a, b = rng.gauss(0, 1), rng.gauss(0, 1)
        bad = 1 if 2.0 * a - 1.5 * b + rng.gauss(0, 0.7) > 0 else 0
        rows.append((str(a), str(b), str(bad), "train" if i < 900 else "test"))
    c.executemany("INSERT INTO ml_sandbox.tape VALUES (%s,%s,%s,%s)", rows)
    yield c
    conn.close()


def _create(cur, name, sql):
    cur.execute(f"DROP MODEL IF EXISTS {name}")
    cur.execute(sql)
    assert wait_for_model(cur, name, timeout=600) == "Model is Ready"


def _one(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def _show(cur, name):
    cur.execute(f"SHOW MODEL {name}")
    return dict(cur.fetchall())


# -----------------------------------------------------------------------------
# Every model type x problem type
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("model_type", MODEL_TYPES)
def test_regression(cur, model_type):
    name = f"reg_{model_type.lower()}"
    fn = f"predict_{name}"
    _create(
        cur,
        name,
        f"CREATE MODEL {name} FROM (SELECT sqft, beds, price FROM ml_homes) "
        f"TARGET price FUNCTION {fn} IAM_ROLE default MODEL_TYPE {model_type}",
    )
    cur.execute(f"SELECT {fn}(sqft, beds) AS pred, price FROM ml_homes")
    errors = [abs(pred - actual) / actual for pred, actual in cur.fetchall()]
    assert max(errors) < 0.1, f"{model_type} max rel error {max(errors):.3f}"


@pytest.mark.parametrize("model_type", MODEL_TYPES)
def test_classification(cur, model_type):
    name = f"clf_{model_type.lower()}"
    fn = f"predict_{name}"
    _create(
        cur,
        name,
        f"CREATE MODEL {name} FROM (SELECT score, income, approved FROM ml_apps) "
        f"TARGET approved FUNCTION {fn} IAM_ROLE default MODEL_TYPE {model_type}",
    )
    assert _one(cur, f"SELECT {fn}(720, 80)") == 1.0
    assert _one(cur, f"SELECT {fn}(610, 20)") == 0.0


@pytest.mark.parametrize("model_type", MODEL_TYPES)
def test_multiclass(cur, model_type):
    name = f"mc_{model_type.lower()}"
    fn = f"predict_{name}"
    _create(
        cur,
        name,
        f"CREATE MODEL {name} FROM (SELECT x1, x2, cls FROM ml_iris) "
        f"TARGET cls FUNCTION {fn} IAM_ROLE default MODEL_TYPE {model_type} "
        "PROBLEM_TYPE multiclass_classification",
    )
    preds = {x1: _one(cur, f"SELECT {fn}({x1}, 2.0)") for x1 in (1.0, 5.0, 9.0)}
    assert preds == {1.0: 0.0, 5.0: 1.0, 9.0: 2.0}, f"{model_type}: {preds}"


def test_autopilot_selects_and_predicts(cur):
    fn = "predict_autopilot"
    # no MODEL_TYPE -> Autopilot; classes {0,1,2} -> auto-detected multiclass
    _create(
        cur,
        "autopilot_iris",
        "CREATE MODEL autopilot_iris FROM ml_iris TARGET cls FUNCTION "
        f"{fn} IAM_ROLE default",
    )
    show = _show(cur, "autopilot_iris")
    assert show["Problem Type"] == "multiclass_classification"
    assert show["AutoML"] == "ON"
    candidates = {k: float(v) for k, v in show.items() if k.upper() in MODEL_TYPES}
    assert set(candidates) == {t.lower() for t in MODEL_TYPES}
    assert show["Model Type"] in candidates
    assert candidates[show["Model Type"]] == max(candidates.values())
    preds = {x1: _one(cur, f"SELECT {fn}({x1}, 2.0)") for x1 in (1.0, 5.0, 9.0)}
    assert preds == {1.0: 0.0, 5.0: 1.0, 9.0: 2.0}
    # Autopilot classification also gets <fn>_probabilities
    probs = _one(cur, f"SELECT {fn}_probabilities(9.0, 2.0)")
    probs = probs if isinstance(probs, dict) else json.loads(probs)
    assert probs["labels"] == ["0", "1", "2"]
    assert abs(sum(probs["probabilities"]) - 1.0) < 1e-5  # rounded to 6 places
    assert probs["probabilities"][2] == max(probs["probabilities"])


# -----------------------------------------------------------------------------
# The AUTO OFF XGBoost workflow (schema-qualified, varchar tape, SHOW MODEL)
# -----------------------------------------------------------------------------
AUTO_OFF = """
create model ml_sandbox.credit_demo
from (
    select f1::double precision as f_a, f2::double precision as f_b, target::int as target
    from ml_sandbox.tape where sample = 'train'
)
target target
function credit_predict
iam_role 'arn:aws:iam::000000000000:role/redshift-ml'
auto off
model_type xgboost
objective '{objective}'
preprocessors 'none'
hyperparameters default except (num_round '80', max_depth '3', eta '0.2', subsample '0.9')
settings (s3_bucket 'ml-bucket', max_runtime 900)
"""


def test_auto_off_xgboost_is_async_and_shows_its_report(cur):
    cur.execute("DROP MODEL IF EXISTS ml_sandbox.credit_demo")
    cur.execute(AUTO_OFF.format(objective="binary:logistic"))
    # CREATE MODEL returns before training: the model is listed as TRAINING
    cur.execute(
        "SELECT trim(model_state) FROM svv_ml_model_info "
        "WHERE trim(schema_name) = 'ml_sandbox' AND trim(model_name) = 'credit_demo'"
    )
    assert cur.fetchone()[0] in ("TRAINING", "Model is Ready")
    assert wait_for_model(cur, "ml_sandbox.credit_demo") == "Model is Ready"

    show = _show(cur, "ml_sandbox.credit_demo")
    assert show["Model State"] == "READY"
    assert show["Function Parameters"] == "f_a f_b"
    assert show["Function Parameter Types"] == "float8 float8"
    assert show["AutoML"] == "OFF" and show["Objective"] == "binary:logistic"
    assert show["eta"] == "0.2" and show["num_round"] == "80"
    assert float(show["validation:accuracy"]) > 0.8

    # the function lands in the model's schema and returns the class label
    cur.execute(
        "SELECT p.pronargs FROM pg_proc p JOIN pg_namespace n "
        "ON n.oid = p.pronamespace WHERE n.nspname = 'ml_sandbox' "
        "AND p.proname = 'credit_predict'"
    )
    assert cur.fetchone()[0] == 2
    accuracy = _one(
        cur,
        "SELECT avg(CASE WHEN ml_sandbox.credit_predict(f1::float8, f2::float8) "
        "= target::float8 THEN 1.0 ELSE 0 END) FROM ml_sandbox.tape "
        "WHERE sample = 'test'",
    )
    assert accuracy > 0.8
    cur.execute("SHOW MODEL ALL")
    assert ("ml_sandbox", "credit_demo") in cur.fetchall()


def test_reg_logistic_predicts_a_probability(cur):
    # binary:logistic returns labels; reg:logistic returns P(target = 1)
    cur.execute("DROP MODEL IF EXISTS ml_sandbox.credit_demo")
    cur.execute(AUTO_OFF.format(objective="reg:logistic"))
    wait_for_model(cur, "ml_sandbox.credit_demo")
    cur.execute(
        "SELECT ml_sandbox.credit_predict(f1::float8, f2::float8), target::int "
        "FROM ml_sandbox.tape WHERE sample = 'test'"
    )
    scored = cur.fetchall()
    assert all(0.0 < p < 1.0 for p, _ in scored)
    bads = [p for p, y in scored if y == 1]
    goods = [p for p, y in scored if y == 0]
    assert sum(bads) / len(bads) > sum(goods) / len(goods) + 0.3


# -----------------------------------------------------------------------------
# What Redshift rejects synchronously, DROP MODEL, and the Data API
# -----------------------------------------------------------------------------
def test_auto_off_rejects_too_few_rows(cur):
    cur.execute("DROP MODEL IF EXISTS small_model")
    with pytest.raises(psycopg2.Error, match="needs at least 500"):
        cur.execute(
            "CREATE MODEL small_model FROM ml_apps TARGET approved FUNCTION f_small "
            "IAM_ROLE default AUTO OFF MODEL_TYPE XGBOOST OBJECTIVE 'binary:logistic' "
            "PREPROCESSORS 'none' HYPERPARAMETERS DEFAULT"
        )


def test_auto_off_rejects_a_missing_clause(cur):
    with pytest.raises(psycopg2.Error, match="AUTO OFF requires PREPROCESSORS"):
        cur.execute(
            "CREATE MODEL m FROM ml_apps TARGET approved FUNCTION f IAM_ROLE default "
            "AUTO OFF MODEL_TYPE XGBOOST OBJECTIVE 'binary:logistic' "
            "HYPERPARAMETERS DEFAULT"
        )


def test_non_numeric_features_are_rejected(cur):
    with pytest.raises(psycopg2.Error, match="needs numeric features"):
        cur.execute(
            "CREATE MODEL m FROM (SELECT f1, target FROM ml_sandbox.tape) "
            "TARGET target FUNCTION f IAM_ROLE default"
        )


def test_drop_model_removes_its_functions(cur):
    _create(
        cur,
        "drop_me",
        "CREATE MODEL drop_me FROM (SELECT score, income, approved FROM ml_apps) "
        "TARGET approved FUNCTION predict_drop_me IAM_ROLE default "
        "MODEL_TYPE LINEAR_LEARNER",
    )
    assert _one(cur, "SELECT to_regproc('predict_drop_me') IS NOT NULL")
    cur.execute("DROP MODEL drop_me")
    assert not _one(cur, "SELECT to_regproc('predict_drop_me') IS NOT NULL")
    assert not _one(
        cur, "SELECT to_regproc('predict_drop_me_probabilities') IS NOT NULL"
    )
    cur.execute("SELECT count(*) FROM svv_ml_model_info WHERE model_name = 'drop_me'")
    assert cur.fetchone()[0] == 0
    with pytest.raises(psycopg2.Error, match="does not exist"):
        cur.execute("DROP MODEL drop_me")
    cur.execute("DROP MODEL IF EXISTS drop_me")  # no error


def test_data_api_create_model(cur):
    executor = RedshiftDataExecutor(
        host="localhost", port=5439, user="oblako", password="oblako", database="oblako"
    )
    cur.execute("DROP MODEL IF EXISTS api_model")
    stmt = executor.execute(
        "CREATE MODEL api_model FROM (SELECT score, income, approved FROM ml_apps) "
        "TARGET approved FUNCTION predict_api IAM_ROLE default MODEL_TYPE XGBOOST"
    )
    assert executor.describe(stmt)["Status"] == "FINISHED"
    wait_for_model(cur, "api_model")
    assert _one(cur, "SELECT predict_api(720, 80)") == 1.0
