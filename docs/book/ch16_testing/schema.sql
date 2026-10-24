CREATE TABLE {schema}.daily_totals (
    day      varchar(10),
    customer varchar(32),
    orders   bigint,
    revenue  decimal(12, 2)
) DISTKEY(customer) SORTKEY(day);
