import boto3

dynamodb = boto3.client("dynamodb")
query = [{"N": "1.0"}, {"N": "0.1"}, {"N": "0.0"}]


def search(condition, values):
    found = dynamodb.search_vectors(
        TableName="products",
        IndexName="similar",
        SearchVector=query,
        TopK=3,
        SearchConditionExpression=condition,
        ExpressionAttributeValues=values,
    )
    for hit in found["SearchResults"]:
        title = hit["Item"]["title"]["S"]
        print(f"{hit['Score']:.4f} {title}")


search("category = :c", {":c": {"S": "kitchen"}})
print("--")
search(
    "category = :c AND brand = :b",
    {":c": {"S": "kitchen"}, ":b": {"S": "forge"}},
)
