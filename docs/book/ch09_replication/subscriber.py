from db import PASSWORD, USER, rds, wait

rds.create_db_instance(
    DBInstanceIdentifier="shop-analytics",
    Engine="postgres",
    EngineVersion="16",
    DBInstanceClass="db.t4g.micro",
    AllocatedStorage=20,
    MasterUsername=USER,
    MasterUserPassword=PASSWORD,
    DBName="shop",
)
db = wait("shop-analytics")
print(db["DBInstanceIdentifier"], "is", db["DBInstanceStatus"])
