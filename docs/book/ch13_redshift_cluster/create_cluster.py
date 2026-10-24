import boto3

redshift = boto3.client("redshift")
redshift.create_cluster(
    ClusterIdentifier="analytics",
    ClusterType="multi-node",
    NodeType="ra3.large",
    NumberOfNodes=2,
    MasterUsername="admin",
    MasterUserPassword="Analytics-local-1",
    DBName="dev",
)
waiter = redshift.get_waiter("cluster_available")
waiter.wait(ClusterIdentifier="analytics")

found = redshift.describe_clusters(ClusterIdentifier="analytics")
cluster = found["Clusters"][0]
print(cluster["ClusterStatus"], cluster["NumberOfNodes"], "nodes")
print(cluster["Endpoint"]["Address"])
for node in cluster["ClusterNodes"]:
    print(node["NodeRole"], node["PrivateIPAddress"])
