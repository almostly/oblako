import boto3
from pyspark.sql import SparkSession

ICEBERG = "1.10.0"
PACKAGES = [
    f"org.apache.iceberg:{name}:{ICEBERG}"
    for name in (
        "iceberg-spark-runtime-4.0_2.13",
        "iceberg-aws-bundle",
    )
]
S3_FILE_IO = "org.apache.iceberg.aws.s3.S3FileIO"


def bucket_arn(name="lakehouse"):
    s3tables = boto3.client("s3tables")
    found = s3tables.list_table_buckets(prefix=name)
    return found["tableBuckets"][0]["arn"]


def session():
    # the JVM does not read the profile's endpoints, so ask boto3
    aws = boto3.Session()
    region = aws.region_name
    tables = aws.client("s3tables").meta.endpoint_url
    s3 = aws.client("s3")
    lake = "spark.sql.catalog.lake"
    builder = (
        SparkSession.builder.appName("lakehouse")
        .config("spark.jars.packages", ",".join(PACKAGES))
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.ui.showConsoleProgress", "false")
        .config(
            "spark.sql.extensions",
            "org.apache.iceberg.spark.extensions"
            ".IcebergSparkSessionExtensions",
        )
        .config(lake, "org.apache.iceberg.spark.SparkCatalog")
        .config(f"{lake}.type", "rest")
        .config(f"{lake}.uri", f"{tables}/iceberg")
        .config(f"{lake}.warehouse", bucket_arn())
        .config(f"{lake}.rest.sigv4-enabled", "true")
        .config(f"{lake}.rest.signing-name", "s3tables")
        .config(f"{lake}.rest.signing-region", region)
        .config(f"{lake}.io-impl", S3_FILE_IO)
    )
    endpoint, s3_config = s3.meta.endpoint_url, s3.meta.config.s3
    builder = builder.config(f"{lake}.s3.endpoint", endpoint)
    if s3_config.get("addressing_style") == "path":
        path_style = f"{lake}.s3.path-style-access"
        builder = builder.config(path_style, "true")
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")
    return spark
