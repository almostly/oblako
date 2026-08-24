"""Pipeline step 2: read step 1's output and double every value."""
import os

values = [float(x) for x in open("/opt/ml/processing/input/data.csv").read().split()]
os.makedirs("/opt/ml/processing/output", exist_ok=True)
with open("/opt/ml/processing/output/doubled.csv", "w") as fh:
    fh.write("\n".join(str(v * 2) for v in values))
print("doubled", len(values), "values")
