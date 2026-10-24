import time

import boto3

athena = boto3.client("athena")
DONE = ("SUCCEEDED", "FAILED", "CANCELLED")


def run_query(sql, database="web"):
    query_id = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": database},
        WorkGroup="primary",
    )["QueryExecutionId"]
    while True:
        execution = athena.get_query_execution(
            QueryExecutionId=query_id
        )
        status = execution["QueryExecution"]["Status"]
        if status["State"] in DONE:
            break
        time.sleep(0.5)
    if status["State"] != "SUCCEEDED":
        raise RuntimeError(status.get("StateChangeReason"))
    results = athena.get_query_results(QueryExecutionId=query_id)
    rows = results["ResultSet"]["Rows"]
    return [
        [col.get("VarCharValue") for col in row["Data"]]
        for row in rows
    ]
