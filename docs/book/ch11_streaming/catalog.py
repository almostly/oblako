import boto3

glue = boto3.client("glue")
try:
    glue.create_database(DatabaseInput={"Name": "web"})
except glue.exceptions.AlreadyExistsException:
    pass

HIVE = "org.apache.hadoop.hive.ql.io.parquet"
STORAGE = {
    "InputFormat": f"{HIVE}.MapredParquetInputFormat",
    "OutputFormat": f"{HIVE}.MapredParquetOutputFormat",
    "SerdeInfo": {
        "SerializationLibrary": f"{HIVE}.serde.ParquetHiveSerDe"
    },
}
glue.create_table(
    DatabaseName="web",
    TableInput={
        "Name": "stream_clicks",
        "TableType": "EXTERNAL_TABLE",
        "PartitionKeys": [{"Name": "dt", "Type": "string"}],
        "StorageDescriptor": {
            "Columns": [
                {"Name": "user", "Type": "string"},
                {"Name": "page", "Type": "string"},
                {"Name": "at", "Type": "timestamp"},
            ],
            "Location": "s3://events/stream-clicks/",
            **STORAGE,
        },
    },
)
print("table web.stream_clicks")
