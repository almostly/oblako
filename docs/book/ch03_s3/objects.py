import boto3

s3 = boto3.client("s3")
for day in ["2026-09-01", "2026-09-02", "2026-09-03"]:
    for n in range(3):
        key = f"raw/{day}/click-{n:04d}.json"
        s3.put_object(Bucket="events", Key=key, Body=b"{}")

resp = s3.list_objects_v2(
    Bucket="events", Prefix="raw/", Delimiter="/"
)
for prefix in resp["CommonPrefixes"]:
    print(prefix["Prefix"])

resp = s3.list_objects_v2(
    Bucket="events", Prefix="raw/2026-09-02/"
)
print([obj["Key"] for obj in resp["Contents"]])
