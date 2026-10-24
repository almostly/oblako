import json

import boto3

sqs = boto3.client("sqs")
events = boto3.client("events")
queue = sqs.create_queue(QueueName="file-arrivals")["QueueUrl"]
queue_arn = sqs.get_queue_attributes(
    QueueUrl=queue, AttributeNames=["QueueArn"]
)["Attributes"]["QueueArn"]

events.put_rule(
    Name="orders-landed",
    EventPattern=json.dumps(
        {
            "source": ["shop.ingest"],
            "detail-type": ["File landed"],
        }
    ),
)
events.put_targets(
    Rule="orders-landed",
    Targets=[{"Id": "queue", "Arn": queue_arn}],
)
events.put_events(
    Entries=[
        {
            "Source": "shop.ingest",
            "DetailType": "File landed",
            "Detail": json.dumps(
                {"key": "orders/date=2026-09-30/"}
            ),
        }
    ]
)
msg = sqs.receive_message(QueueUrl=queue, WaitTimeSeconds=5)
body = json.loads(msg["Messages"][0]["Body"])
print(body["detail-type"], body["detail"])
