from datetime import timedelta

from feast import (
    Entity,
    FeatureStore,
    FeatureView,
    Field,
    RepoConfig,
    ValueType,
)
from feast.infra.offline_stores.redshift import (
    RedshiftOfflineStoreConfig,
)
from feast.infra.offline_stores.redshift_source import (
    RedshiftSource,
)
from feast.infra.online_stores.dynamodb import (
    DynamoDBOnlineStoreConfig,
)
from feast.types import Float64, Int64

FEATURES = ["customer_stats:orders_7d", "customer_stats:spend_7d"]

customer = Entity(
    name="customer",
    join_keys=["customer_id"],
    value_type=ValueType.INT64,
)

customer_stats = FeatureView(
    name="customer_stats",
    entities=[customer],
    ttl=timedelta(days=2),
    schema=[
        Field(name="orders_7d", dtype=Int64),
        Field(name="spend_7d", dtype=Float64),
    ],
    source=RedshiftSource(
        name="customer_stats_source",
        table="customer_stats",
        timestamp_field="event_timestamp",
        created_timestamp_column="created",
    ),
    online=True,
)

config = RepoConfig(
    project="shop",
    provider="aws",
    registry="s3://feature-staging/feast/registry.db",
    offline_store=RedshiftOfflineStoreConfig(
        cluster_id="analytics",
        region="us-east-1",
        database="oblako",
        user="oblako",
        s3_staging_location="s3://feature-staging/feast",
        iam_role="arn:aws:iam::123456789012:role/redshift-copy",
    ),
    online_store=DynamoDBOnlineStoreConfig(region="us-east-1"),
    entity_key_serialization_version=3,
)


def open_store():
    return FeatureStore(config=config)
