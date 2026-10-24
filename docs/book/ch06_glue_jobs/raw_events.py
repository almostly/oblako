import json

import boto3

s3 = boto3.client("s3")
s3.create_bucket(Bucket="etl-demo")

clicks = [
    ("2026-09-01", "u-1", "/", 820),
    ("2026-09-01", "u-2", "/pricing", 1430),
    ("2026-09-01", "u-3", "/", 2650),
    ("2026-09-02", "u-1", "/docs", 2210),
    ("2026-09-02", "u-4", "/", 450),
    ("2026-09-03", "u-2", "/docs", 1980),
]
lines = [
    json.dumps({"day": d, "user": u, "page": p, "ms": ms})
    for d, u, p, ms in clicks
]
s3.put_object(
    Bucket="etl-demo",
    Key="raw/clicks/batch-001.json",
    Body="\n".join(lines).encode(),
)
print(len(lines), "events")
