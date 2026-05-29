"""Example 12: Redshift ML — CREATE MODEL, trained on SageMaker local, served in-DB.

`CREATE MODEL ... FROM (SELECT ...) TARGET col FUNCTION fn` exports the rows,
trains a real model in a SageMaker local container (scikit-learn), and registers
a plpython3u prediction UDF in pgredshift. Then `SELECT fn(...)` is real
in-database inference. Fully local — no cloud.

Prerequisites:
    pip install 'oblako[sagemaker]'
    make up          # pgredshift engine (5439) + Docker for SageMaker local
"""

import time

from oblako.services import RedshiftService

rs = RedshiftService()

# Seed training tables in pgredshift -----------------------------------------
conn = rs.connect()
conn.autocommit = True
cur = conn.cursor()
cur.execute("DROP TABLE IF EXISTS homes")
cur.execute("CREATE TABLE homes (sqft FLOAT, beds FLOAT, price FLOAT)")
cur.executemany(
    "INSERT INTO homes VALUES (%s, %s, %s)",
    [(s, b, 100.0 * s + 5000.0 * b) for s in (800, 1000, 1200, 1500, 2000, 2500) for b in (1, 2, 3, 4)],
)
cur.execute("DROP TABLE IF EXISTS applicants")
cur.execute("CREATE TABLE applicants (score FLOAT, income FLOAT, approved FLOAT)")
cur.executemany(
    "INSERT INTO applicants VALUES (%s, %s, %s)",
    [(sc, inc, 1.0 if (sc > 660 and inc > 40) else 0.0)
     for sc in range(600, 760, 10) for inc in (20, 35, 50, 80)],
)
cur.execute("DROP TABLE IF EXISTS plants")
cur.execute("CREATE TABLE plants (petal FLOAT, sepal FLOAT, species FLOAT)")
cur.executemany(
    "INSERT INTO plants VALUES (%s, %s, %s)",
    [(p * 0.5, s, 0.0 if p * 0.5 < 3.5 else (1.0 if p * 0.5 < 7.0 else 2.0))
     for p in range(2, 20) for s in (1.0, 2.0, 3.0)],
)
cur.close()
conn.close()

rd = rs.get_data_client()  # boto3 'redshift-data'; auto-starts the server
ARN = dict(Database="oblako")


def run(sql):
    sid = rd.execute_statement(Sql=sql, **ARN)["Id"]
    for _ in range(300):  # CREATE MODEL trains synchronously (SageMaker local)
        desc = rd.describe_statement(Id=sid)
        if desc["Status"] in ("FINISHED", "FAILED"):
            break
        time.sleep(1)
    if desc["Status"] == "FAILED":
        raise RuntimeError(desc.get("Error"))
    return sid


# Regression: predict house price with XGBoost (Redshift's AUTO OFF syntax) ---
print("Training XGBoost regression model (SageMaker local)...")
run("CREATE MODEL price_model FROM (SELECT sqft, beds, price FROM homes) "
    "TARGET price FUNCTION predict_price "
    "AUTO OFF MODEL_TYPE xgboost OBJECTIVE 'reg:squarederror'")
sid = run("SELECT sqft, beds, ROUND(predict_price(sqft, beds)::numeric, 0) AS predicted, price "
          "FROM homes ORDER BY sqft, beds LIMIT 4")
res = rd.get_statement_result(Id=sid)
print("price predictions (predicted vs actual):")
for rec in res["Records"]:
    vals = [list(f.values())[0] for f in rec]
    print(f"sqft={vals[0]} beds={vals[1]} -> {vals[2]} (actual {vals[3]})")

# Binary classification: predict loan approval with an MLP --------------------
print("\nTraining MLP classification model (SageMaker local)...")
run("CREATE MODEL approve_model FROM (SELECT score, income, approved FROM applicants) "
    "TARGET approved FUNCTION predict_approval "
    "MODEL_TYPE MLP PROBLEM_TYPE binary_classification")
sid = run("SELECT score, income, predict_approval(score, income) AS predicted, approved "
          "FROM applicants WHERE score IN (620, 700) AND income IN (20, 80) ORDER BY score, income")
res = rd.get_statement_result(Id=sid)
print("approval predictions (predicted vs actual):")
for rec in res["Records"]:
    vals = [list(f.values())[0] for f in rec]
    print(f"score={vals[0]} income={vals[1]} -> {vals[2]} (actual {vals[3]})")

# Multiclass + Autopilot: no MODEL_TYPE -> trains all three, keeps the best ---
# The 3-class target is auto-detected as multiclass; AUTO ON is the default.
print("\nTraining with Autopilot (trains LINEAR_LEARNER + MLP + XGBOOST)...")
run("CREATE MODEL species_model FROM (SELECT petal, sepal, species FROM plants) "
    "TARGET species FUNCTION predict_species")
sid = run("SELECT petal, predict_species(petal, 2.0) AS predicted "
          "FROM (SELECT 1.0 AS petal UNION SELECT 5.0 UNION SELECT 9.0) q ORDER BY petal")
res = rd.get_statement_result(Id=sid)
print("species predictions (0=small, 1=medium, 2=large petal):")
for rec in res["Records"]:
    vals = [list(f.values())[0] for f in rec]
    print(f"petal={vals[0]} -> class {vals[1]}")
