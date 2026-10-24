from warehouse import query

query("DROP TABLE IF EXISTS page_views")
query(
    "CREATE TABLE page_views ("
    " day date ENCODE az64,"
    " user_id varchar(16),"
    " page varchar(max),"
    " load_ms integer"
    ") DISTKEY(user_id) SORTKEY(day)"
)
print(
    query(
        'SELECT "column", type FROM pg_table_def'
        " WHERE tablename = 'page_views'"
    )
)
