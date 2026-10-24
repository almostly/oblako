from warehouse import query

rows = query(
    "SELECT user_id,"
    " LISTAGG(page, ', ') WITHIN GROUP (ORDER BY day) AS path,"
    " DATEDIFF(day, min(day), max(day)) AS days_active"
    " FROM page_views GROUP BY user_id ORDER BY user_id"
)
for row in rows:
    print(row)

print(query("SELECT DATEADD(month, 1, '2026-09-30'::date)"))
