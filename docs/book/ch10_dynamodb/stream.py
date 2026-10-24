from collections import Counter

import boto3

dynamodb = boto3.client("dynamodb")
streams = boto3.client("dynamodbstreams")

table = dynamodb.describe_table(TableName="orders")["Table"]
arn = table["LatestStreamArn"]
described = streams.describe_stream(StreamArn=arn)
records = []
for shard in described["StreamDescription"]["Shards"]:
    iterator = streams.get_shard_iterator(
        StreamArn=arn,
        ShardId=shard["ShardId"],
        ShardIteratorType="TRIM_HORIZON",
    )["ShardIterator"]
    batch = streams.get_records(ShardIterator=iterator)
    records += batch["Records"]

print(Counter(r["eventName"] for r in records))
for record in records:
    if record["eventName"] == "MODIFY":
        change = record["dynamodb"]
        old = change["OldImage"]["status"]["S"]
        new = change["NewImage"]["status"]["S"]
        print(change["Keys"]["order_id"]["S"], old, "->", new)
