import json
from collections import defaultdict

import boto3

kinesis = boto3.client("kinesis")
stream = kinesis.describe_stream(StreamName="clicks")[
    "StreamDescription"
]

for shard in stream["Shards"]:
    iterator = kinesis.get_shard_iterator(
        StreamName="clicks",
        ShardId=shard["ShardId"],
        ShardIteratorType="TRIM_HORIZON",
    )["ShardIterator"]
    records = kinesis.get_records(ShardIterator=iterator)[
        "Records"
    ]
    users = defaultdict(int)
    for record in records:
        users[json.loads(record["Data"])["user"]] += 1
    print(shard["ShardId"], len(records), "records")
    for user, count in sorted(users.items()):
        print(f"  {user}: {count}")
