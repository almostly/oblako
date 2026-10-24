import json
import time

import boto3

s3 = boto3.client("s3")
sqs = boto3.client("sqs")

queue_url = sqs.create_queue(QueueName="new-events")["QueueUrl"]
queue_arn = sqs.get_queue_attributes(
    QueueUrl=queue_url, AttributeNames=["QueueArn"]
)["Attributes"]["QueueArn"]

s3.put_bucket_notification_configuration(
    Bucket="events",
    NotificationConfiguration={
        "QueueConfigurations": [
            {
                "QueueArn": queue_arn,
                "Events": ["s3:ObjectCreated:*"],
                "Filter": {
                    "Key": {
                        "FilterRules": [
                            {
                                "Name": "prefix",
                                "Value": "incoming/",
                            }
                        ]
                    }
                },
            }
        ]
    },
)

s3.put_object(
    Bucket="events", Key="incoming/batch-001.json", Body=b"{}"
)

for _ in range(10):
    messages = sqs.receive_message(
        QueueUrl=queue_url, WaitTimeSeconds=1
    ).get("Messages", [])
    if messages:
        record = json.loads(messages[0]["Body"])["Records"][0]
        print(record["eventName"], record["s3"]["object"]["key"])
        break
    time.sleep(1)
