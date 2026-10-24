import boto3

dynamodb = boto3.client("dynamodb")
for name in ("orders", "products"):
    try:
        dynamodb.delete_table(TableName=name)
    except dynamodb.exceptions.ResourceNotFoundException:
        pass
