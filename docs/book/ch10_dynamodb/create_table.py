import boto3

dynamodb = boto3.client("dynamodb")
dynamodb.create_table(
    TableName="orders",
    AttributeDefinitions=[
        {"AttributeName": "customer", "AttributeType": "S"},
        {"AttributeName": "order_id", "AttributeType": "S"},
        {"AttributeName": "status", "AttributeType": "S"},
    ],
    KeySchema=[
        {"AttributeName": "customer", "KeyType": "HASH"},
        {"AttributeName": "order_id", "KeyType": "RANGE"},
    ],
    GlobalSecondaryIndexes=[
        {
            "IndexName": "by-status",
            "KeySchema": [
                {"AttributeName": "status", "KeyType": "HASH"},
                {"AttributeName": "order_id", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ],
    BillingMode="PAY_PER_REQUEST",
    StreamSpecification={
        "StreamEnabled": True,
        "StreamViewType": "NEW_AND_OLD_IMAGES",
    },
)
dynamodb.get_waiter("table_exists").wait(TableName="orders")
table = dynamodb.describe_table(TableName="orders")["Table"]
billing = table["BillingModeSummary"]["BillingMode"]
print(table["TableStatus"], billing)
print("stream:", table["StreamSpecification"]["StreamViewType"])
