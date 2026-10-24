from decimal import Decimal

import boto3

orders = boto3.resource("dynamodb").Table("orders")
with orders.batch_writer() as batch:
    for n in range(1, 13):
        batch.put_item(
            Item={
                "customer": f"customer-{n % 3}",
                "order_id": f"2026-10-{n:02d}#{n:04d}",
                "total": Decimal(n * 125) / 10,
                "status": "shipped" if n % 4 else "placed",
            }
        )
print("items:", orders.scan(Select="COUNT")["Count"])
