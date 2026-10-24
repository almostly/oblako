import sys

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.transforms import ApplyMapping, Filter
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext

args = getResolvedOptions(
    sys.argv, ["JOB_NAME", "source", "target"]
)
glue = GlueContext(SparkContext())
job = Job(glue)
job.init(args["JOB_NAME"], args)

raw = glue.create_dynamic_frame.from_options(
    "s3", {"paths": [args["source"]]}, format="json"
)
typed = ApplyMapping.apply(
    frame=raw,
    mappings=[
        ("day", "string", "day", "string"),
        ("user", "string", "user", "string"),
        ("page", "string", "page", "string"),
        ("ms", "long", "load_ms", "long"),
    ],
)
fast = Filter.apply(
    frame=typed, f=lambda row: row["load_ms"] < 2000
)
glue.write_dynamic_frame.from_options(
    fast,
    "s3",
    {"path": args["target"], "partitionKeys": ["day"]},
    format="parquet",
)
print("rows in:", raw.count(), "rows out:", fast.count())
job.commit()
