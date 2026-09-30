#!/usr/bin/env python3
"""SageMaker 'bring your own container' training entry point.

SageMaker local mode runs this image as `docker run <image> train`. It reads
CSV (x,y) from /opt/ml/input/data/train/, fits y = slope*x + intercept by least
squares (pure Python, no deps), and writes the model to /opt/ml/model/.
SageMaker then tars /opt/ml/model into model.tar.gz at the output path.
"""

import glob
import json
import os
import sys

INPUT = "/opt/ml/input/data/train"
MODEL = "/opt/ml/model"


def main():
    xs, ys = [], []
    for path in sorted(glob.glob(os.path.join(INPUT, "*.csv"))):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                x, y = line.split(",")
                xs.append(float(x))
                ys.append(float(y))

    n = len(xs)
    if n == 0:
        raise SystemExit("no training data found in /opt/ml/input/data/train")

    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    intercept = (sy - slope * sx) / n

    os.makedirs(MODEL, exist_ok=True)
    with open(os.path.join(MODEL, "model.json"), "w") as fh:
        json.dump({"slope": slope, "intercept": intercept, "rows": n}, fh)
    print(f"Trained on {n} rows: y = {slope:.4f}*x + {intercept:.4f}")


if __name__ == "__main__":
    # SageMaker local mode also runs housekeeping in this image: on Linux the
    # container writes root-owned files into the bind-mounted job dir, and the
    # SDK cleans them up with `<image> chmod -R 777 <dir>`. Run any command other
    # than `train` as given, so that works.
    if len(sys.argv) > 1 and sys.argv[1] != "train":
        os.execvp(sys.argv[1], sys.argv[1:])
    main()
