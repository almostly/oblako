import boto3

dynamodb = boto3.client("dynamodb")


def vector(*values):
    return {"L": [{"N": str(v)} for v in values]}


products = [  # sku, category, brand, title, embedding
    ("k-100", "kitchen", "acme", "Chef's knife", (0.9, 0.1, 0)),
    ("k-101", "kitchen", "acme", "Paring knife", (0.8, 0.2, 0.1)),
    ("k-102", "kitchen", "forge", "Bread knife", (0.7, 0.3, 0)),
    ("k-200", "kitchen", "forge", "Breadboard", (0.2, 0.9, 0.1)),
    ("g-300", "garden", "acme", "Pruning shears", (0.9, 0, 0.2)),
]
for sku, category, brand, title, embedding in products:
    dynamodb.put_item(
        TableName="products",
        Item={
            "sku": {"S": sku},
            "category": {"S": category},
            "brand": {"S": brand},
            "title": {"S": title},
            "embedding": vector(*embedding),
        },
    )
print(len(products), "products")
