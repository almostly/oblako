import json

import boto3

secrets = boto3.client("secretsmanager")
secret_arn = secrets.create_secret(
    Name="catalog/oblako",
    SecretString=json.dumps(
        {"username": "oblako", "password": "oblako"}
    ),
)["ARN"]
rds = boto3.client("rds")
cluster = rds.describe_db_clusters(DBClusterIdentifier="catalog")
cluster_arn = cluster["DBClusters"][0]["DBClusterArn"]

rds_data = boto3.client("rds-data")
target = {
    "resourceArn": cluster_arn,
    "secretArn": secret_arn,
    "database": "oblako",
}

tx = rds_data.begin_transaction(**target)["transactionId"]
rds_data.execute_statement(
    **target,
    transactionId=tx,
    sql="UPDATE products SET price = price * 1.1"
    " WHERE sku = :sku",
    parameters=[
        {"name": "sku", "value": {"stringValue": "MUG-01"}}
    ],
)
done = rds_data.commit_transaction(
    resourceArn=cluster_arn,
    secretArn=secret_arn,
    transactionId=tx,
)
print(done["transactionStatus"])

result = rds_data.execute_statement(
    **target,
    sql="SELECT sku, price FROM products ORDER BY sku",
    formatRecordsAs="JSON",
)
print(json.loads(result["formattedRecords"]))
