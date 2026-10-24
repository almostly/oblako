import boto3

glue = boto3.client("glue")
s3 = boto3.client("s3")

table = glue.get_table(DatabaseName="web", Name="stream_clicks")[
    "Table"
]
listing = s3.list_objects_v2(
    Bucket="events", Prefix="stream-clicks/", Delimiter="/"
)
for common in listing.get("CommonPrefixes", []):
    folder = common["Prefix"]  # stream-clicks/dt=2026-10-02/
    day = folder.rstrip("/").split("dt=")[1]
    storage = dict(table["StorageDescriptor"])
    storage["Location"] = f"s3://events/{folder}"
    try:
        glue.create_partition(
            DatabaseName="web",
            TableName="stream_clicks",
            PartitionInput={
                "Values": [day],
                "StorageDescriptor": storage,
            },
        )
        print("added partition dt =", day)
    except glue.exceptions.AlreadyExistsException:
        print("partition dt =", day, "exists")
