"""Pipeline step 1: write a few numbers to the processing output."""

import os

os.makedirs("/opt/ml/processing/output", exist_ok=True)
with open("/opt/ml/processing/output/data.csv", "w") as fh:
    fh.write("1\n2\n3\n4\n")
print("prepared 4 rows")
