import pyarrow as pa
from catalog import open_catalog
from pyiceberg.types import StringType

table = open_catalog().load_table("web.page_views")
with table.update_schema() as update:
    update.add_column("country", StringType())

new = pa.table(
    {
        "day": ["2026-09-04"],
        "page": ["/docs"],
        "views": [52],
        "country": ["NL"],
    }
)
table.append(new)
rows = table.scan().to_pandas().sort_values("day")
print(rows.tail(3))
