import datetime
import io
import random

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

s3 = boto3.client("s3")
s3.create_bucket(Bucket="warehouse-staging")
rng = random.Random(7)
N = 20000  # orders
day0 = datetime.date(2026, 9, 1)
days = [day0 + datetime.timedelta(d) for d in range(30)]
names = "North South East West Coast Hills Lakes Plains".split()


def amount():
    return round(rng.uniform(5, 250), 2)


tables = {
    "customers": {
        "customer_id": list(range(1, 2001)),
        "region": [f"r{i % 8}" for i in range(1, 2001)],
    },
    "orders": {
        "order_id": list(range(1, N + 1)),
        "customer_id": [rng.randint(1, 2000) for _ in range(N)],
        "order_date": [days[i % 30] for i in range(N)],
        "total": [amount() for _ in range(N)],
    },
    "regions": {
        "region": [f"r{i}" for i in range(8)],
        "name": names,
    },
}
for name, columns in tables.items():
    table = pa.table(columns)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    key = f"{name}/part-0.parquet"
    body = buffer.getvalue()
    s3.put_object(Bucket="warehouse-staging", Key=key, Body=body)
    print(name, table.num_rows, "rows")
