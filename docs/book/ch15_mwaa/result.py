import boto3
from warehouse import run

data = boto3.client("redshift-data")
statement = run("SELECT day, orders, revenue FROM daily_revenue")
for row in data.get_statement_result(Id=statement)["Records"]:
    print([list(value.values())[0] for value in row])
