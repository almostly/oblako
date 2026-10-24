import json

import boto3

s3 = boto3.client("s3")
policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "DenyDelete",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:DeleteObject",
            "Resource": "arn:aws:s3:::events/*",
        }
    ],
}
s3.put_bucket_policy(Bucket="events", Policy=json.dumps(policy))
resp = s3.get_bucket_policy(Bucket="events")
stored = json.loads(resp["Policy"])
print(stored["Statement"][0]["Sid"])

# On AWS this delete is refused with AccessDenied. oblako stores
# the policy but does not evaluate it, so the delete goes through.
s3.delete_object(
    Bucket="events", Key="raw/2026-09-03/click-0002.json"
)
print("deleted")
