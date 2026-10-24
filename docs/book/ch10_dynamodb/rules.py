import boto3

dynamodb = boto3.client("dynamodb")
errors = dynamodb.exceptions

try:  # the vector index partition key must be in every search
    dynamodb.search_vectors(
        TableName="products",
        IndexName="similar",
        SearchVector=[{"N": "1"}, {"N": "0"}, {"N": "0"}],
        TopK=3,
    )
except errors.ClientError as e:
    print(e.response["Error"]["Code"], "on search")

try:  # a vector must have the index's dimensions
    dynamodb.put_item(
        TableName="products",
        Item={
            "sku": {"S": "k-999"},
            "category": {"S": "kitchen"},
            "embedding": {"L": [{"N": "1"}, {"N": "0"}]},
        },
    )
except errors.ClientError as e:
    print(e.response["Error"]["Code"], "on write")
