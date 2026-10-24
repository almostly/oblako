from store import FEATURES, open_store

store = open_store()
features = store.get_online_features(
    features=FEATURES,
    entity_rows=[{"customer_id": 7}, {"customer_id": 21}],
).to_dict()
for key in ("customer_id", "orders_7d", "spend_7d"):
    print(key, features[key])
