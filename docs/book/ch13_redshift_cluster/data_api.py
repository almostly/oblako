import time

import boto3

data = boto3.client("redshift-data")
statement = data.execute_statement(
    ClusterIdentifier="analytics",
    Database="dev",
    DbUser="admin",
    Sql="SELECT count(DISTINCT customer_id) FROM orders",
)
while True:
    status = data.describe_statement(Id=statement["Id"])["Status"]
    if status in ("FINISHED", "FAILED", "ABORTED"):
        break
    time.sleep(0.5)
result = data.get_statement_result(Id=statement["Id"])
print(status, result["Records"])
