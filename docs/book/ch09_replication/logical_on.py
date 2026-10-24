from db import connect, one, rds, wait

rds.create_db_parameter_group(
    DBParameterGroupName="shop-logical",
    DBParameterGroupFamily="postgres16",
    Description="PostgreSQL 16 with logical replication",
)
rds.modify_db_parameter_group(
    DBParameterGroupName="shop-logical",
    Parameters=[
        {
            "ParameterName": "rds.logical_replication",
            "ParameterValue": "1",
            "ApplyMethod": "pending-reboot",
        }
    ],
)
rds.modify_db_instance(
    DBInstanceIdentifier="shop-primary",
    DBParameterGroupName="shop-logical",
    ApplyImmediately=True,
)
wait("shop-primary")
rds.reboot_db_instance(DBInstanceIdentifier="shop-primary")
wait("shop-primary")

with connect("shop-primary") as primary:
    print("wal_level:", one(primary, "SHOW wal_level"))
