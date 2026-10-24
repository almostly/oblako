import json

import boto3

s3 = boto3.client("s3")
s3.create_bucket(Bucket="events")

event = {
    "user": "u-17",
    "action": "signup",
    "at": "2026-09-01T08:15:00Z",
}
s3.put_object(
    Bucket="events",
    Key="raw/2026-09-01/signup-0001.json",
    Body=json.dumps(event).encode(),
    ContentType="application/json",
    Metadata={"source": "web"},
)

key = "raw/2026-09-01/signup-0001.json"
head = s3.head_object(Bucket="events", Key=key)
print(
    head["ContentLength"], head["ContentType"], head["Metadata"]
)

obj = s3.get_object(Bucket="events", Key=key)
print(json.loads(obj["Body"].read()))
