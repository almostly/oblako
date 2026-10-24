from db import connect, one

with connect("shop-primary") as primary:
    bytes_behind, replay_lag = primary.execute("""
        SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn),
               replay_lag
        FROM pg_stat_replication""").fetchone()
    print("bytes behind:", bytes_behind)
    print("replay lag:", replay_lag)

with connect("shop-replica") as replica:
    since = "SELECT now() - pg_last_xact_replay_timestamp()"
    print("last replayed commit:", one(replica, since), "ago")
