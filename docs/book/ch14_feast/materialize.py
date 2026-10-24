from datetime import datetime

import boto3
from store import open_store

store = open_store()
store.materialize(
    start_date=datetime(2026, 9, 1),
    end_date=datetime(2026, 9, 15),
)
dynamodb = boto3.client("dynamodb")
tables = dynamodb.list_tables()["TableNames"]
table = [t for t in tables if t.startswith("shop.")][0]
items = dynamodb.scan(TableName=table, Select="COUNT")["Count"]
print(table, items, "items")
