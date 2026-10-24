import pandas as pd
from store import FEATURES, open_store

store = open_store()
times = [
    "2026-09-05 18:00",
    "2026-09-12 09:30",
    "2026-09-30 12:00",
]
labels = pd.DataFrame(
    {
        "customer_id": [7, 7, 21],
        "event_timestamp": pd.to_datetime(times),
    }
)
training = store.get_historical_features(
    entity_df=labels,
    features=FEATURES,
).to_df()
print(
    training.sort_values(
        ["customer_id", "event_timestamp"]
    ).to_string(index=False)
)
