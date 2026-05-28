"""S3 Tables (Iceberg-on-S3) + Athena (Trino) — daily-sales walkthrough.

Mirrors the AWS S3 Tables console example:

    CREATE TABLE `s3_namespace`.daily_sales (...) PARTITIONED BY (month(sale_date))
    INSERT INTO daily_sales VALUES (...)
    SELECT product_category, COUNT(*), SUM(sales_amount), AVG(sales_amount) ...

Locally the S3 Tables endpoint == oblako's Iceberg REST catalog, and Athena ==
Trino. We use **pyiceberg** for CREATE+INSERT (the Trino S3 write path currently
hits a 501 on S3Proxy's ListObjectsV2) and **Trino** for the SELECT — both see
the same Iceberg catalog, so the queryable result is identical.

Run from the repo root:

    uv run --extra iceberg python examples/athena-s3-tables/daily_sales.py
"""

from __future__ import annotations

import os
from datetime import date

import pyarrow as pa

# S3Proxy needs path-style addressing + the new flexible checksums turned off.
os.environ.update(
    AWS_REQUEST_CHECKSUM_CALCULATION="when_required",
    AWS_RESPONSE_CHECKSUM_VALIDATION="when_required",
)

from pyiceberg.catalog import load_catalog  # noqa: E402

from oblako.services.platform import Oblako  # noqa: E402

NAMESPACE = "s3_namespace"
TABLE = "daily_sales"
WAREHOUSE = "s3://oblako-iceberg/"

ROWS = pa.table({
    "sale_date": [date(2024, 1, 15), date(2024, 1, 15), date(2024, 1, 16),
                  date(2024, 2, 1),  date(2024, 2, 1),  date(2024, 2, 2),
                  date(2024, 2, 2),  date(2024, 2, 3),  date(2024, 2, 3)],
    "product_category": ["Laptop", "Monitor", "Laptop", "Monitor", "Keyboard",
                         "Mouse",  "Laptop",  "Laptop", "Monitor"],
    "sales_amount":     [900.00, 250.00, 1350.00, 300.00, 60.00,
                         25.00,  1050.00, 1200.00, 375.00],
})


def main() -> None:
    o = Oblako()
    o.iceberg.wait_ready(timeout=2) or o.iceberg.start()
    o.trino.wait_ready(timeout=2) or o.trino.start()

    # Ensure the warehouse bucket exists on S3Proxy.
    s3 = o.s3.get_client()
    if "oblako-iceberg" not in {b["Name"] for b in s3.list_buckets().get("Buckets", [])}:
        s3.create_bucket(Bucket="oblako-iceberg")

    cat = load_catalog(
        "oblako", uri="http://localhost:8181", warehouse=WAREHOUSE,
        **{"py-io-impl": "pyiceberg.io.fsspec.FsspecFileIO",
           "s3.endpoint": "http://localhost:9000",
           "s3.access-key-id": "test", "s3.secret-access-key": "test",
           "s3.path-style-access": "true"},
    )

    # 1. Schema + partitioned table (Iceberg's month() transform).
    try:
        cat.drop_table(f"{NAMESPACE}.{TABLE}")
    except Exception:  # noqa: BLE001
        pass
    try:
        cat.drop_namespace(NAMESPACE)
    except Exception:  # noqa: BLE001
        pass
    cat.create_namespace(NAMESPACE)

    from pyiceberg.schema import Schema
    from pyiceberg.types import DateType, DoubleType, NestedField, StringType
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.transforms import MonthTransform

    schema = Schema(
        NestedField(1, "sale_date", DateType(), required=False),
        NestedField(2, "product_category", StringType(), required=False),
        NestedField(3, "sales_amount", DoubleType(), required=False),
    )
    partition_spec = PartitionSpec(PartitionField(
        source_id=1, field_id=1000, transform=MonthTransform(), name="sale_month",
    ))
    tbl = cat.create_table(f"{NAMESPACE}.{TABLE}", schema=schema, partition_spec=partition_spec)

    # 2. INSERT INTO equivalent (parquet on S3Proxy).
    tbl.append(ROWS)
    print(f"wrote {ROWS.num_rows} rows to {NAMESPACE}.{TABLE}")

    # 3. Athena-style aggregation through Trino.
    result = o.trino.query(f"""
        SELECT product_category,
               COUNT(*)         AS units_sold,
               SUM(sales_amount) AS total_revenue,
               AVG(sales_amount) AS average_price
        FROM iceberg.{NAMESPACE}.{TABLE}
        WHERE sale_date BETWEEN DATE '2024-02-01' AND DATE '2024-02-29'
        GROUP BY product_category
        ORDER BY total_revenue DESC
    """)
    print("\n=== Athena query result ===")
    print(" | ".join(result["columns"]))
    for row in result["rows"]:
        print(" | ".join(str(v) for v in row))

    # Also drop a flat parquet at a stable URL for the dashboard's DuckDB-Wasm
    # starter query (Iceberg's partitioned filenames have UUIDs in them, so they
    # aren't quotable as a fixed URL).
    import io
    import pyarrow.parquet as pq
    buf = io.BytesIO()
    pq.write_table(ROWS, buf)
    s3.put_object(Bucket="oblako-iceberg", Key="demos/daily_sales.parquet", Body=buf.getvalue())
    print("\nwrote demos/daily_sales.parquet (stable URL for dashboard demos)")


if __name__ == "__main__":
    main()
