import boto3

ecs = boto3.client("ecs")
ecs.create_cluster(clusterName="batch")
ecs.register_task_definition(
    family="count-rows",
    requiresCompatibilities=["FARGATE"],
    networkMode="awsvpc",
    cpu="256",
    memory="512",
    containerDefinitions=[
        {
            "name": "job",
            "image": "public.ecr.aws/docker/library/busybox:1.36",
            "command": ["sh", "-c", "echo 3 rows; exit 0"],
            "essential": True,
        }
    ],
)
task = ecs.run_task(
    cluster="batch",
    taskDefinition="count-rows",
    launchType="FARGATE",
    networkConfiguration={
        "awsvpcConfiguration": {
            "subnets": ["subnet-a"],
            "assignPublicIp": "ENABLED",
        }
    },
)["tasks"][0]
ecs.get_waiter("tasks_stopped").wait(
    cluster="batch", tasks=[task["taskArn"]]
)
done = ecs.describe_tasks(
    cluster="batch", tasks=[task["taskArn"]]
)
for t in done["tasks"]:
    container = t["containers"][0]
    print(
        t["lastStatus"], container["name"], container["exitCode"]
    )
