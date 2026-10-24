import boto3

eks = boto3.client("eks")
eks.create_cluster(
    name="analytics",
    roleArn="arn:aws:iam::123456789012:role/eks",
    resourcesVpcConfig={"subnetIds": ["subnet-a", "subnet-b"]},
)
cluster = eks.describe_cluster(name="analytics")["cluster"]
print(cluster["name"], cluster["status"], cluster["version"])
