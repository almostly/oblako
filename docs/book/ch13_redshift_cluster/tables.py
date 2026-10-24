from cluster import query

ROLE = "arn:aws:iam::123456789012:role/redshift-copy"
query("""
    CREATE TABLE customers (
        customer_id integer,
        region      varchar(4))
    DISTKEY(customer_id) SORTKEY(customer_id)""")
query("""
    CREATE TABLE orders (
        order_id    integer,
        customer_id integer,
        order_date  date,
        total       decimal(10, 2))
    DISTKEY(customer_id) SORTKEY(order_date)""")
query("""
    CREATE TABLE regions (
        region varchar(4),
        name   varchar(16))
    DISTSTYLE ALL""")
for table in ("customers", "orders", "regions"):
    query(f"""
        COPY {table} FROM 's3://warehouse-staging/{table}/'
        IAM_ROLE '{ROLE}' FORMAT AS PARQUET""")
print(query("SELECT count(*) FROM orders"))
