import boto3

s3vectors = boto3.client("s3vectors")
s3vectors.create_vector_bucket(vectorBucketName="docs-vectors")
s3vectors.create_index(
    vectorBucketName="docs-vectors",
    indexName="pages",
    dataType="float32",
    dimension=3,
    distanceMetric="cosine",
)


def page(key, vector, section):
    return {
        "key": key,
        "data": {"float32": vector},
        "metadata": {"section": section},
    }


s3vectors.put_vectors(
    vectorBucketName="docs-vectors",
    indexName="pages",
    vectors=[
        page("pricing", [0.9, 0.1, 0.0], "sales"),
        page("quickstart", [0.1, 0.9, 0.2], "docs"),
        page("api", [0.0, 0.8, 0.6], "docs"),
    ],
)

resp = s3vectors.query_vectors(
    vectorBucketName="docs-vectors",
    indexName="pages",
    queryVector={"float32": [0.1, 1.0, 0.3]},
    topK=2,
    filter={"section": "docs"},
    returnDistance=True,
    returnMetadata=True,
)
for hit in resp["vectors"]:
    print(hit["key"], round(hit["distance"], 4), hit["metadata"])
