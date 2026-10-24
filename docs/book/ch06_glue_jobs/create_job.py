import boto3

s3 = boto3.client("s3")
glue = boto3.client("glue")

s3.upload_file(
    "clean_clicks.py", "etl-demo", "scripts/clean_clicks.py"
)
glue.create_job(
    Name="clean-clicks",
    Role="arn:aws:iam::123456789012:role/glue-etl",
    Command={
        "Name": "glueetl",
        "ScriptLocation": "s3://etl-demo/scripts/clean_clicks.py",
        "PythonVersion": "3",
    },
    DefaultArguments={
        "--source": "s3://etl-demo/raw/clicks/",
        "--target": "s3://etl-demo/clean/clicks/",
    },
    GlueVersion="5.0",
    WorkerType="G.1X",
    NumberOfWorkers=2,
)
print(glue.list_jobs()["JobNames"])
