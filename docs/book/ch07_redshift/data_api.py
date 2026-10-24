import time

import boto3

rsd = boto3.client("redshift-data")
statement = rsd.execute_statement(
    ClusterIdentifier="analytics",
    Database="oblako",
    DbUser="oblako",
    Sql="SELECT page, avg(load_ms) AS avg_ms,"
    " avg(load_ms::float) AS exact_ms FROM page_views"
    " GROUP BY page ORDER BY avg_ms DESC",
)
while True:
    desc = rsd.describe_statement(Id=statement["Id"])
    if desc["Status"] in ("FINISHED", "FAILED", "ABORTED"):
        break
    time.sleep(0.5)
if desc["Status"] != "FINISHED":
    raise RuntimeError(desc.get("Error", desc["Status"]))

result = rsd.get_statement_result(Id=statement["Id"])
print([col["name"] for col in result["ColumnMetadata"]])
for record in result["Records"]:
    print([list(field.values())[0] for field in record])
