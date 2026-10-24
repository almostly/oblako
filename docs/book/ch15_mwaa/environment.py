import time

import boto3

mwaa = boto3.client("mwaa")
mwaa.create_environment(
    Name="shop",
    AirflowVersion="3.3.1",
    EnvironmentClass="mw1.micro",
    SourceBucketArn="arn:aws:s3:::shop-dags",
    DagS3Path="dags",
    ExecutionRoleArn="arn:aws:iam::123456789012:role/mwaa",
    NetworkConfiguration={
        "SubnetIds": ["subnet-a", "subnet-b"],
        "SecurityGroupIds": ["sg-airflow"],
    },
    WebserverAccessMode="PUBLIC_ONLY",
)
while True:
    env = mwaa.get_environment(Name="shop")["Environment"]
    if env["Status"] != "CREATING":
        break
    time.sleep(15)
print(env["Name"], env["Status"], env["AirflowVersion"])
