import boto3

athena = boto3.client("athena")
group = athena.get_work_group(WorkGroup="primary")["WorkGroup"]
results = group["Configuration"]["ResultConfiguration"]
print(results["OutputLocation"])
