import time

import boto3

data = boto3.client("redshift-data")


def run(sql):
    statement = data.execute_statement(
        ClusterIdentifier="analytics",
        Database="oblako",
        DbUser="oblako",
        Sql=sql,
    )
    while True:
        desc = data.describe_statement(Id=statement["Id"])
        if desc["Status"] in ("FINISHED", "FAILED", "ABORTED"):
            break
        time.sleep(0.2)
    if desc["Status"] != "FINISHED":
        raise RuntimeError(desc.get("Error"))
    return statement["Id"]
