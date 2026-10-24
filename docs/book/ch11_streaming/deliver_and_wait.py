import time

import boto3

from clicks import as_records, clicks

kinesis = boto3.client("kinesis")
s3 = boto3.client("s3")

kinesis.put_records(
    StreamName="clicks", Records=as_records(clicks(200, seed=3))
)
started = time.monotonic()
objects = []
while not objects:
    time.sleep(5)
    listing = s3.list_objects_v2(
        Bucket="events", Prefix="stream-clicks/"
    )
    objects = listing.get("Contents", [])
waited = time.monotonic() - started
for obj in objects:
    folder, name = obj["Key"].rsplit("/", 1)
    print(folder + "/")
    print("  " + name[:34] + "..." + name[-8:])
print(f"delivered after about {waited:.0f} s")
