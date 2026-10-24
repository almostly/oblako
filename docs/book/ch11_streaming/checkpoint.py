import boto3

from clicks import as_records, clicks

kinesis = boto3.client("kinesis")
shard = kinesis.describe_stream(StreamName="clicks")
shard_id = shard["StreamDescription"]["Shards"][0]["ShardId"]


def read(**position):
    iterator = kinesis.get_shard_iterator(
        StreamName="clicks", ShardId=shard_id, **position
    )["ShardIterator"]
    return kinesis.get_records(ShardIterator=iterator)["Records"]


seen = read(ShardIteratorType="TRIM_HORIZON")
checkpoint = seen[-1][
    "SequenceNumber"
]  # store this after processing
print("processed", len(seen), "up to", checkpoint[-6:])

kinesis.put_records(
    StreamName="clicks", Records=as_records(clicks(10, 2))
)
new = read(
    ShardIteratorType="AFTER_SEQUENCE_NUMBER",
    StartingSequenceNumber=checkpoint,
)
print("new since the checkpoint:", len(new))
