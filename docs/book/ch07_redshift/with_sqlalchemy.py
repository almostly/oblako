import pandas as pd
import sqlalchemy as sa

url = "redshift+redshift_connector://oblako:oblako@localhost:5439"
engine = sa.create_engine(
    f"{url}/oblako",
    connect_args={"ssl": True, "sslmode": "verify-ca"},
)

columns = sa.inspect(engine).get_columns("page_views")
print([(c["name"], str(c["type"])) for c in columns])

page_views = sa.Table(
    "page_views", sa.MetaData(), autoload_with=engine
)
query = (
    sa.select(page_views.c.page, sa.func.count().label("views"))
    .group_by(page_views.c.page)
    .order_by(sa.desc("views"), page_views.c.page)
)
with engine.connect() as conn:
    for row in conn.execute(query):
        print(row)

daily = pd.read_sql(
    "SELECT day, avg(load_ms) AS avg_ms"
    " FROM page_views GROUP BY day",
    engine,
)
print(daily.sort_values("day").to_string(index=False))
