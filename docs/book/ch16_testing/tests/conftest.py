import os
import uuid
from pathlib import Path

import boto3
import pytest

from pipeline import connect_redshift

REDSHIFT = {
    "host": os.environ.get("REDSHIFT_HOST", "localhost"),
    "port": int(os.environ.get("REDSHIFT_PORT", "5439")),
    "database": os.environ.get("REDSHIFT_DATABASE", "oblako"),
    "user": os.environ.get("REDSHIFT_USER", "oblako"),
    "password": os.environ.get("REDSHIFT_PASSWORD", "oblako"),
}
ROLE = os.environ.get(
    "REDSHIFT_COPY_ROLE",
    "arn:aws:iam::123456789012:role/redshift-copy",
)
RUN = uuid.uuid4().hex[:8]  # one id per test session


@pytest.fixture(scope="session")
def connect():
    return connect_redshift(**REDSHIFT)


@pytest.fixture(scope="session")
def bucket():
    s3 = boto3.client("s3")
    name = f"daily-totals-test-{RUN}"
    s3.create_bucket(Bucket=name)
    yield name
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=name
    ):
        for obj in page.get("Contents", []):
            s3.delete_object(Bucket=name, Key=obj["Key"])
    s3.delete_bucket(Bucket=name)


@pytest.fixture(scope="session")
def schema(connect):
    name = f"test_{RUN}"
    ddl = Path(__file__).parent.parent / "schema.sql"
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(f"CREATE SCHEMA {name}")
        cur.execute(ddl.read_text().format(schema=name))
        conn.commit()
    yield name
    with connect() as conn:
        conn.cursor().execute(f"DROP SCHEMA {name} CASCADE")
        conn.commit()
