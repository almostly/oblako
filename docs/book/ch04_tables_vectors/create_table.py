import pyarrow as pa
from catalog import open_catalog

catalog = open_catalog()
views = pa.table(
    {
        "day": ["2026-09-01", "2026-09-01", "2026-09-02"],
        "page": ["/", "/pricing", "/docs"],
        "views": [120, 31, 45],
    }
)
schema = views.schema
table = catalog.create_table("web.page_views", schema=schema)
table.append(views)

print(catalog.list_tables("web"))
print(table.scan(row_filter="views > 40").to_pandas())
