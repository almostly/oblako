import boto3

orders = boto3.resource("dynamodb").Table("orders")
errors = orders.meta.client.exceptions
key = {"customer": "customer-1", "order_id": "2026-10-04#0004"}


def ship():
    orders.update_item(
        Key=key,
        UpdateExpression="SET #s = :shipped",
        ConditionExpression="#s = :placed",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":placed": "placed",
            ":shipped": "shipped",
        },
    )


ship()
print("shipped once")
try:
    ship()
except errors.ConditionalCheckFailedException:
    print("second ship refused: the order is no longer placed")
