import boto3

s3 = boto3.client("s3")
for n in range(1200):
    key = f"many/day=2026-09-04/part-{n:05d}.json"
    s3.put_object(Bucket="events", Key=key, Body=b"{}")

paginator = s3.get_paginator("list_objects_v2")
pages = paginator.paginate(Bucket="events", Prefix="many/")
sizes = [len(page["Contents"]) for page in pages]
print(sizes, sum(sizes))
