import time

import boto3

mwaa = boto3.client("mwaa")


def rest(path, method="GET", body=None):
    extra = {"Body": body} if body is not None else {}
    resp = mwaa.invoke_rest_api(
        Name="shop", Path=path, Method=method, **extra
    )
    return resp["RestApiResponse"]


while "daily_orders" not in [
    d["dag_id"] for d in rest("/dags")["dags"]
]:
    time.sleep(15)  # the DAG processor has not parsed it yet
rest("/dags/daily_orders", "PATCH", {"is_paused": False})
run = rest(
    "/dags/daily_orders/dagRuns",
    "POST",
    {"logical_date": "2026-09-30T00:00:00Z"},
)
path = f"/dags/daily_orders/dagRuns/{run['dag_run_id']}"
while rest(path)["state"] not in ("success", "failed"):
    time.sleep(10)
tasks = rest(path + "/taskInstances")["task_instances"]
for t in sorted(tasks, key=lambda t: t["start_date"]):
    print(t["task_id"], t["state"])
