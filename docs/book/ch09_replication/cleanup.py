from db import rds

for name in ("shop-analytics", "shop-replica", "shop-primary"):
    try:
        rds.delete_db_instance(
            DBInstanceIdentifier=name, SkipFinalSnapshot=True
        )
    except rds.exceptions.DBInstanceNotFoundFault:
        pass
try:
    rds.delete_db_parameter_group(
        DBParameterGroupName="shop-logical"
    )
except rds.exceptions.DBParameterGroupNotFoundFault:
    pass
