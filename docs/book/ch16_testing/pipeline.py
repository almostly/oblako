import io
import json
from collections import defaultdict
from decimal import Decimal

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import redshift_connector


def summarise(events):
    """Total each customer's orders; refunds subtract."""
    totals = defaultdict(lambda: [0, Decimal("0")])
    for event in events:
        amount = Decimal(event["amount"])
        if event["type"] == "refund":
            amount = -amount
        else:
            totals[event["customer"]][0] += 1
        totals[event["customer"]][1] += amount
    return [
        {"customer": c, "orders": n, "revenue": r}
        for c, (n, r) in sorted(totals.items())
    ]


def read_events(bucket, day):
    s3 = boto3.client("s3")
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"raw/orders/date={day}/"
    )
    for page in pages:
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])[
                "Body"
            ]
            for line in body.iter_lines():
                yield json.loads(line)


def load(conn, bucket, day, rows, schema, role):
    table = pa.Table.from_pylist(
        [{"day": day, **row} for row in rows],
        schema=pa.schema(
            [
                ("day", pa.string()),
                ("customer", pa.string()),
                ("orders", pa.int64()),
                ("revenue", pa.decimal128(12, 2)),
            ]
        ),
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    key = f"staging/daily_totals/date={day}/part-0.parquet"
    boto3.client("s3").put_object(
        Bucket=bucket, Key=key, Body=buffer.getvalue()
    )
    cur = conn.cursor()
    cur.execute(
        f"DELETE FROM {schema}.daily_totals WHERE day = %s",
        (day,),
    )
    cur.execute(
        f"COPY {schema}.daily_totals FROM 's3://{bucket}/{key}'"
        f" IAM_ROLE '{role}' FORMAT AS PARQUET"
    )
    conn.commit()


def run(day, *, bucket, schema, role, connect):
    rows = summarise(read_events(bucket, day))
    with connect() as conn:
        load(conn, bucket, day, rows, schema, role)
    return len(rows)


def connect_redshift(host, port, database, user, password):
    return lambda: redshift_connector.connect(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        ssl=True,
        sslmode="verify-ca",
    )
