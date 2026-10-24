import boto3
from boto3.dynamodb.conditions import Key

orders = boto3.resource("dynamodb").Table("orders")

key = {"customer": "customer-1", "order_id": "2026-10-04#0004"}
order = orders.get_item(Key=key)["Item"]
print("get:", order["status"], order["total"])

early = orders.query(
    KeyConditionExpression=Key("customer").eq("customer-1")
    & Key("order_id").begins_with("2026-10-0"),
)
for o in early["Items"]:
    print("customer-1:", o["order_id"], o["status"])

placed = orders.query(
    IndexName="by-status",
    KeyConditionExpression=Key("status").eq("placed"),
)
print("placed:", [o["customer"] for o in placed["Items"]])
