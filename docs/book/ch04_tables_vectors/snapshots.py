import pyarrow as pa
from catalog import open_catalog

table = open_catalog().load_table("web.page_views")
first = table.current_snapshot().snapshot_id

more = pa.table(
    {"day": ["2026-09-03"], "page": ["/"], "views": [98]}
)
table.append(more)

for entry in table.history():
    print(entry.snapshot_id, entry.timestamp_ms)

print("now:", table.scan().to_arrow().num_rows, "rows")
then = table.scan(snapshot_id=first).to_arrow()
print("then:", then.num_rows, "rows")
