from collections import Counter

import boto3

from clicks import as_records, clicks

kinesis = boto3.client("kinesis")
result = kinesis.put_records(
    StreamName="clicks", Records=as_records(clicks(40, seed=1))
)
print("failed:", result["FailedRecordCount"])
print(Counter(r["ShardId"] for r in result["Records"]))
