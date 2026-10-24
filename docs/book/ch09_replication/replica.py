from db import rds, wait

rds.create_db_instance_read_replica(
    DBInstanceIdentifier="shop-replica",
    SourceDBInstanceIdentifier="shop-primary",
)
db = wait("shop-replica")
source = db["ReadReplicaSourceDBInstanceIdentifier"]
print(db["DBInstanceStatus"], "replica of", source)
print(db["Endpoint"]["Address"])
