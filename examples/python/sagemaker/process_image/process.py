#!/usr/bin/env python3
"""Minimal SageMaker Processing container.

SageMaker (and oblako) mount inputs under /opt/ml/processing/input and collect
/opt/ml/processing/output to S3. This one reads numbers from the input and writes
each doubled to output/output.csv — a stand-in for a real processing step.
"""

import glob
import os

INPUT = "/opt/ml/processing/input"
OUTPUT = "/opt/ml/processing/output"


def main():
    values = []
    for path in sorted(glob.glob(os.path.join(INPUT, "*"))):
        if not os.path.isfile(path):
            continue
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    values.append(float(line))
    os.makedirs(OUTPUT, exist_ok=True)
    with open(os.path.join(OUTPUT, "output.csv"), "w") as fh:
        for v in values:
            fh.write(f"{v * 2}\n")
    print(f"processed {len(values)} values")


if __name__ == "__main__":
    main()
