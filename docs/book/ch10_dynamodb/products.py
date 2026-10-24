import boto3

dynamodb = boto3.client("dynamodb")
dynamodb.create_table(
    TableName="products",
    AttributeDefinitions=[
        {"AttributeName": "sku", "AttributeType": "S"},
        {"AttributeName": "category", "AttributeType": "S"},
        {"AttributeName": "brand", "AttributeType": "S"},
    ],
    KeySchema=[{"AttributeName": "sku", "KeyType": "HASH"}],
    BillingMode="PAY_PER_REQUEST",
    VectorIndexes=[
        {
            "IndexName": "similar",
            "VectorAttribute": {"AttributeName": "embedding"},
            "SearchSchema": [
                {
                    "AttributeName": "category",
                    "SearchSchemaElementType": "HASH",
                },
                {
                    "AttributeName": "brand",
                    "SearchSchemaElementType": "INLINE_FILTER",
                },
            ],
            "Projection": {"ProjectionType": "ALL"},
            "Dimensions": 3,
            "DistanceFunction": "COSINE",
        }
    ],
)
dynamodb.get_waiter("table_exists").wait(TableName="products")
table = dynamodb.describe_table(TableName="products")["Table"]
for index in table["VectorIndexes"]:
    print(index["IndexName"], index["IndexStatus"])
