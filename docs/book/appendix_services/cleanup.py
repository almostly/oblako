import boto3

boto3.client("lambda").delete_function(FunctionName="order-total")
boto3.client("iam").delete_role(RoleName="order-total")

sfn = boto3.client("stepfunctions")
for machine in sfn.list_state_machines()["stateMachines"]:
    if machine["name"] == "daily-load":
        sfn.delete_state_machine(
            stateMachineArn=machine["stateMachineArn"]
        )

events = boto3.client("events")
events.remove_targets(Rule="orders-landed", Ids=["queue"])
events.delete_rule(Name="orders-landed")
sqs = boto3.client("sqs")
sqs.delete_queue(
    QueueUrl=sqs.get_queue_url(QueueName="file-arrivals")[
        "QueueUrl"
    ]
)

boto3.client("eks").delete_cluster(name="analytics")

ecs = boto3.client("ecs")
for arn in ecs.list_tasks(cluster="batch")["taskArns"]:
    ecs.stop_task(cluster="batch", task=arn)
for arn in ecs.list_task_definitions(familyPrefix="count-rows")[
    "taskDefinitionArns"
]:
    ecs.deregister_task_definition(taskDefinition=arn)
ecs.delete_cluster(cluster="batch")
