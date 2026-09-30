"""Local Redshift ML: CREATE / SHOW / DROP MODEL, run by redshift-local itself.

The implementation lives in the Redshift image (``oblako/images/redshift/proxy/
redshift_ml.py``): the wire proxy rewrites the MODEL statements, CREATE MODEL
validates and queues the model, and an agent in the container trains it in a
SageMaker-style container on the host Docker daemon and publishes the prediction
function. So Redshift ML works from any client (psycopg, DBeaver, dbt, the Data
API), asynchronously, the way Amazon Redshift runs it with SageMaker.

This package loads that same module on the host (parser, codegen, rewrite) and
adds :func:`wait_for_model`, which polls ``svv_ml_model_info`` like a user would.
"""

from __future__ import annotations

import importlib.util
import pathlib
import time

_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "images"
    / "redshift"
    / "proxy"
    / "redshift_ml.py"
)
_spec = importlib.util.spec_from_file_location("oblako_redshift_ml", _PATH)
_impl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_impl)

MLError = _impl.MLError
is_create_model = _impl.is_create_model
parse_create_model = _impl.parse_create_model
detect_problem_type = _impl.detect_problem_type
rewrite_ml = _impl.rewrite_ml
udf_sql = _impl.udf_sql
UDF_BODIES = _impl.UDF_BODIES
linear_body = _impl.linear_body
mlp_body = _impl.mlp_body
xgb_body = _impl.xgb_body

__all__ = [
    "MLError",
    "UDF_BODIES",
    "detect_problem_type",
    "is_create_model",
    "linear_body",
    "mlp_body",
    "parse_create_model",
    "rewrite_ml",
    "udf_sql",
    "wait_for_model",
    "xgb_body",
]


def wait_for_model(
    cursor, model: str, timeout: float = 900.0, interval: float = 2.0
) -> str:
    """Poll ``svv_ml_model_info`` until ``model`` leaves TRAINING.

    ``model`` may be ``schema.name``. Returns the final ``model_state``
    ("Model is Ready") and raises ``RuntimeError`` with the failure reason if
    training failed, or ``TimeoutError`` if it is still training at ``timeout``.
    """
    schema, _, name = model.rpartition(".")
    sql = "SELECT trim(model_state) FROM svv_ml_model_info WHERE model_name = %s"
    params: tuple = (name,)
    if schema:
        sql += " AND trim(schema_name) = %s"
        params = (name, schema)
    deadline = time.time() + timeout
    while True:
        cursor.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"model {model!r} is not in svv_ml_model_info")
        state = row[0]
        if state == "Model is Ready":
            return state
        if state != "TRAINING":
            raise RuntimeError(f"model {model!r} failed: {state}")
        if time.time() > deadline:
            raise TimeoutError(f"model {model!r} still training after {timeout}s")
        time.sleep(interval)
