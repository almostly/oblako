from db import PASSWORD, USER, rds, wait

rds.create_db_instance(
    DBInstanceIdentifier="shop-primary",
    Engine="postgres",
    EngineVersion="16",
    DBInstanceClass="db.t4g.micro",
    AllocatedStorage=20,
    MasterUsername=USER,
    MasterUserPassword=PASSWORD,
    DBName="shop",
    BackupRetentionPeriod=1,
)
db = wait("shop-primary")
print(db["DBInstanceStatus"], db["Engine"], db["EngineVersion"])
print(db["Endpoint"]["Address"])
