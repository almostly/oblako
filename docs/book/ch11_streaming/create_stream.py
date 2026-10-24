import boto3

kinesis = boto3.client("kinesis")
kinesis.create_stream(StreamName="clicks", ShardCount=2)
kinesis.get_waiter("stream_exists").wait(StreamName="clicks")

stream = kinesis.describe_stream(StreamName="clicks")[
    "StreamDescription"
]
print(
    stream["StreamStatus"],
    stream["RetentionPeriodHours"],
    "hours",
)
for shard in stream["Shards"]:
    hashes = shard["HashKeyRange"]
    start = int(hashes["StartingHashKey"])
    end = int(hashes["EndingHashKey"])
    print(shard["ShardId"], f"{start:.3e}", f"{end:.3e}")
