import boto3

glue = boto3.client("glue")
glue.create_database(DatabaseInput={"Name": "web"})

hive = "org.apache.hadoop.hive.ql.io.parquet"
parquet = {
    "InputFormat": f"{hive}.MapredParquetInputFormat",
    "OutputFormat": f"{hive}.MapredParquetOutputFormat",
    "SerdeInfo": {
        "SerializationLibrary": f"{hive}.serde.ParquetHiveSerDe"
    },
}
glue.create_table(
    DatabaseName="web",
    TableInput={
        "Name": "clicks",
        "TableType": "EXTERNAL_TABLE",
        "PartitionKeys": [{"Name": "day", "Type": "string"}],
        "StorageDescriptor": {
            "Columns": [
                {"Name": "user", "Type": "string"},
                {"Name": "page", "Type": "string"},
                {"Name": "ms", "Type": "bigint"},
            ],
            "Location": "s3://lake-data/clicks/",
            **parquet,
        },
    },
)
days = ["2026-09-01", "2026-09-02", "2026-09-03"]
glue.batch_create_partition(
    DatabaseName="web",
    TableName="clicks",
    PartitionInputList=[
        {
            "Values": [day],
            "StorageDescriptor": {
                "Location": f"s3://lake-data/clicks/day={day}/",
                **parquet,
            },
        }
        for day in days
    ],
)
parts = glue.get_partitions(
    DatabaseName="web", TableName="clicks"
)
print(sorted(p["Values"][0] for p in parts["Partitions"]))
