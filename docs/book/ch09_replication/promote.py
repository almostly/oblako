from db import connect, one, rds, wait

rds.promote_read_replica(DBInstanceIdentifier="shop-replica")
db = wait("shop-replica")
source = db.get("ReadReplicaSourceDBInstanceIdentifier")
print(db["DBInstanceStatus"], "source:", source)

COUNT = "SELECT count(*) FROM orders"
RECOVERY = "SELECT pg_is_in_recovery()"
with connect("shop-replica") as promoted:
    print("in recovery:", one(promoted, RECOVERY))
    promoted.execute("DELETE FROM orders WHERE id = 1001")
    print("orders:", one(promoted, COUNT))

with connect("shop-primary") as primary:
    print("primary still has:", one(primary, COUNT))
