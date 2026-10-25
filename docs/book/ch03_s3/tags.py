import boto3

s3 = boto3.client("s3")
key = "raw/2026-09-01/signup-0001.json"

s3.put_object_tagging(
    Bucket="events",
    Key=key,
    Tagging={
        "TagSet": [
            {"Key": "stage", "Value": "raw"},
            {"Key": "pii", "Value": "no"},
        ]
    },
)
print(s3.get_object_tagging(Bucket="events", Key=key)["TagSet"])

# A second put replaces the whole set; it does not merge.
s3.put_object_tagging(
    Bucket="events",
    Key=key,
    Tagging={"TagSet": [{"Key": "stage", "Value": "clean"}]},
)
print(s3.get_object_tagging(Bucket="events", Key=key)["TagSet"])
