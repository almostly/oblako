import datetime
import io
import random

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from warehouse import run

s3 = boto3.client("s3")
s3.create_bucket(Bucket="feature-staging")
rng = random.Random(3)
day0 = datetime.datetime(2026, 9, 1)

rows = []
for customer in range(1, 51):
    for day in range(14):
        orders = rng.randint(0, 9)
        spend = round(orders * rng.uniform(10, 60), 2)
        at = day0 + datetime.timedelta(days=day)
        rows.append(
            {
                "customer_id": customer,
                "event_timestamp": at,
                "orders_7d": orders,
                "spend_7d": spend,
                "created": at + datetime.timedelta(hours=1),
            }
        )
buffer = io.BytesIO()
pq.write_table(pa.Table.from_pylist(rows), buffer)
s3.put_object(
    Bucket="feature-staging",
    Key="customer_stats/part-0.parquet",
    Body=buffer.getvalue(),
)

run("""
    CREATE TABLE customer_stats (
        customer_id     integer,
        event_timestamp timestamp,
        orders_7d       bigint,
        spend_7d        double precision,
        created         timestamp)
    DISTKEY(customer_id) SORTKEY(event_timestamp)""")
run("""
    COPY customer_stats
    FROM 's3://feature-staging/customer_stats/'
    IAM_ROLE 'arn:aws:iam::123456789012:role/redshift-copy'
    FORMAT AS PARQUET""")
print(len(rows), "rows in customer_stats")
