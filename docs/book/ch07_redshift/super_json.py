from warehouse import query

query("DROP TABLE IF EXISTS raw_events")
query("CREATE TABLE raw_events (payload SUPER)")
query(
    "INSERT INTO raw_events VALUES"
    ' (JSON_PARSE(\'{"page": "/",'
    ' "device": {"os": "ios"}}\')),'
    ' (JSON_PARSE(\'{"page": "/docs",'
    ' "device": {"os": "linux"}}\'))'
)
print(
    query(
        "SELECT payload.page::varchar, payload.device.os::varchar"
        " FROM raw_events ORDER BY 1"
    )
)
