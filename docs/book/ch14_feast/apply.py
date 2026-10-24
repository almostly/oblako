from store import customer, customer_stats, open_store

store = open_store()
store.apply([customer, customer_stats])
for view in store.list_feature_views():
    fields = [field.name for field in view.features]
    print(view.name, view.ttl, fields)
