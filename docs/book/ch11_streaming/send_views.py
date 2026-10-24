import json
import time

import boto3

from clicks import clicks
from warehouse import query

firehose = boto3.client("firehose")
events = list(clicks(50, seed=4))
firehose.put_record_batch(
    DeliveryStreamName="views-to-redshift",
    Records=[{"Data": json.dumps(e).encode()} for e in events],
)

count = 0
while count < len(events):
    time.sleep(5)
    count = query("SELECT count(*) FROM page_views")[0][0]
print("rows in page_views:", count)
print(
    query("""
    SELECT "user", count(*) FROM page_views
    GROUP BY 1 ORDER BY 2 DESC LIMIT 3""")
)
