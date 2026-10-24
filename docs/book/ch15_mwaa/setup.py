import boto3
from warehouse import run

s3 = boto3.client("s3")
for bucket in ("shop-raw", "shop-dags"):
    s3.create_bucket(Bucket=bucket)

run("""CREATE TABLE orders (
    day date, order_id integer, customer integer,
    amount decimal(10, 2))""")
run("""CREATE TABLE daily_revenue (
    day date, orders bigint, revenue decimal(12, 2))""")

rows = ["day,order_id,customer,amount"]
rows += [
    f"2026-09-30,{n},{n % 7},{10 + n}.50" for n in range(1, 41)
]
prefix = "orders/date=2026-09-30/"
s3.put_object(
    Bucket="shop-raw",
    Key=prefix + "orders.csv",
    Body="\n".join(rows).encode(),
)
s3.put_object(
    Bucket="shop-raw", Key=prefix + "_SUCCESS", Body=b""
)
s3.upload_file(
    "dags/daily_orders.py", "shop-dags", "dags/daily_orders.py"
)
print("staged", len(rows) - 1, "orders and the DAG")
