import json
from decimal import Decimal as D

import boto3
import pytest

from pipeline import run
from tests.conftest import ROLE
from tests.test_summarise import event

pytestmark = pytest.mark.integration


def put_events(bucket, day, part, events):
    body = "\n".join(json.dumps(e) for e in events).encode()
    key = f"raw/orders/date={day}/part-{part}.jsonl"
    s3 = boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=key, Body=body)


def totals(connect, schema, day):
    sql = (
        "SELECT customer, orders, revenue"
        f" FROM {schema}.daily_totals"
        " WHERE day = %s ORDER BY customer"
    )
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(sql, (day,))
        return [tuple(row) for row in cur.fetchall()]


def test_loads_a_day_from_two_files(bucket, schema, connect):
    day = "2026-10-01"
    put_events(
        bucket,
        day,
        0,
        [
            event("c1", "order", "10.00"),
            event("c2", "order", "5.00"),
        ],
    )
    put_events(bucket, day, 1, [event("c1", "refund", "2.50")])
    loaded = run(
        day,
        bucket=bucket,
        schema=schema,
        role=ROLE,
        connect=connect,
    )
    assert loaded == 2
    assert totals(connect, schema, day) == [
        ("c1", 1, D("7.50")),
        ("c2", 1, D("5.00")),
    ]


def test_rerunning_a_day_replaces_it(bucket, schema, connect):
    day = "2026-10-02"
    put_events(bucket, day, 0, [event("c1", "order", "3.00")])
    for _ in range(2):
        run(
            day,
            bucket=bucket,
            schema=schema,
            role=ROLE,
            connect=connect,
        )
    assert totals(connect, schema, day) == [("c1", 1, D("3.00"))]
    # the other day's rows are untouched
    assert len(totals(connect, schema, "2026-10-01")) == 2
