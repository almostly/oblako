import boto3

s3 = boto3.client("s3")
s3.create_bucket(Bucket="first-bucket")
s3.put_object(Bucket="first-bucket", Key="notes/today.txt", Body=b"oblako is up")
for obj in s3.list_objects_v2(Bucket="first-bucket")["Contents"]:
    print("s3:", obj["Key"], obj["Size"])

dynamodb = boto3.client("dynamodb")
dynamodb.create_table(
    TableName="first-table",
    KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
    BillingMode="PAY_PER_REQUEST",
)
dynamodb.put_item(TableName="first-table", Item={"id": {"S": "1"}, "note": {"S": "hello"}})
item = dynamodb.get_item(TableName="first-table", Key={"id": {"S": "1"}})["Item"]
print("dynamodb:", item["note"]["S"])

identity = boto3.client("sts").get_caller_identity()
print("sts:", identity["Account"], identity["Arn"])
