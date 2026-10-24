import boto3

rds = boto3.client("rds")
rds.create_db_cluster(
    DBClusterIdentifier="catalog",
    Engine="aurora-postgresql",
    EngineVersion="16.4",
    MasterUsername="oblako",
    MasterUserPassword="oblako-local-1",
    DatabaseName="oblako",
    EnableHttpEndpoint=True,
)
rds.create_db_instance(
    DBInstanceIdentifier="catalog-writer",
    DBClusterIdentifier="catalog",
    DBInstanceClass="db.r6g.large",
    Engine="aurora-postgresql",
)
cluster = rds.describe_db_clusters(DBClusterIdentifier="catalog")
c = cluster["DBClusters"][0]
print(c["Status"], c["Engine"], c["EngineVersion"])
print("writer", c["Endpoint"])
print("reader", c["ReaderEndpoint"])
members = c["DBClusterMembers"]
print("members:", [m["DBInstanceIdentifier"] for m in members])
