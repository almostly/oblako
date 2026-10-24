import datetime

from airflow.providers.amazon.aws.operators.redshift_data import (
    RedshiftDataOperator,
)
from airflow.providers.amazon.aws.sensors.s3 import S3KeySensor
from airflow.sdk import dag

BUCKET = "shop-raw"
DATA_API = {
    "cluster_identifier": "analytics",
    "database": "oblako",
    "db_user": "oblako",
    "wait_for_completion": True,
}


@dag(
    schedule="@daily",
    start_date=datetime.datetime(2026, 9, 1),
    catchup=False,
    tags=["shop"],
)
def daily_orders():
    landed = S3KeySensor(
        task_id="orders_landed",
        bucket_name=BUCKET,
        bucket_key="orders/date={{ ds }}/_SUCCESS",
        poke_interval=10,
        timeout=600,
    )
    load = RedshiftDataOperator(
        task_id="load_orders",
        sql=[
            "DELETE FROM orders WHERE day = '{{ ds }}'",
            "COPY orders FROM "
            "'s3://shop-raw/orders/date={{ ds }}/orders.csv' "
            "IAM_ROLE 'arn:aws:iam::123456789012:role/copy' "
            "CSV IGNOREHEADER 1",
        ],
        **DATA_API,
    )
    summarise = RedshiftDataOperator(
        task_id="daily_revenue",
        sql=[
            "DELETE FROM daily_revenue WHERE day = '{{ ds }}'",
            "INSERT INTO daily_revenue "
            "SELECT day, count(*), sum(amount) FROM orders "
            "WHERE day = '{{ ds }}' GROUP BY day",
        ],
        **DATA_API,
    )
    landed >> load >> summarise


daily_orders()
