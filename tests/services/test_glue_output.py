"""A Glue job's S3 output: the data files only, as AWS Glue 5.0 writes it.

Checked on AWS Glue 5.0 (2026-10): a partitioned write through Spark's writer or
Glue's leaves only the Parquet files, with no directory markers (``day=.../``)
and no ``_SUCCESS``. The integration test needs the ~5 GB Glue image locally and
S3 running; it skips otherwise (CI doesn't pull the image).
"""

import uuid

import pytest

from oblako.services.glue import _SPARK_CONFS, IMAGE_TAG, GlueService

SCRIPT = """
import sys
from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext

args = getResolvedOptions(sys.argv, ["target"])
glue = GlueContext(SparkContext.getOrCreate())
df = glue.spark_session.createDataFrame(
    [(1, "/", "2026-10-01"), (2, "/docs", "2026-10-02")], ["id", "page", "day"]
)
df.write.mode("overwrite").partitionBy("day").parquet(args["target"] + "spark/")
glue.write_dynamic_frame.from_options(
    frame=DynamicFrame.fromDF(df, glue, "clicks"),
    connection_type="s3",
    connection_options={"path": args["target"] + "glue/", "partitionKeys": ["day"]},
    format="parquet",
)
"""


def test_s3a_deletes_directory_markers():
    assert "spark.hadoop.fs.s3a.directory.marker.retention=delete" in _SPARK_CONFS


def _ready() -> bool:
    try:
        from oblako.services import S3ProxyService

        S3ProxyService().get_client().list_buckets()
        GlueService().client.images.get(IMAGE_TAG)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ready(), reason="needs the Glue 5 image locally and S3")
def test_partitioned_output_is_data_files_only():
    from oblako.services import S3ProxyService

    s3 = S3ProxyService().get_client()
    bucket, prefix = "glue-output-test", f"{uuid.uuid4().hex[:8]}/"
    try:
        s3.create_bucket(Bucket=bucket)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    out = GlueService().submit_job(SCRIPT, args=["--target", f"s3://{bucket}/{prefix}"])
    assert out["exit_code"] == 0, out["logs"][-2000:]
    keys = [
        o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix=prefix)["Contents"]
    ]
    assert keys and all(k.endswith(".snappy.parquet") for k in keys), keys
    assert {k.split("/")[2] for k in keys} == {"day=2026-10-01", "day=2026-10-02"}
