#!/usr/bin/env python3
"""SageMaker Model Monitor analyzer (a processing job).

Reads an endpoint's captured invocations (SageMaker Data Capture JSON Lines) from
/opt/ml/processing/input/endpoint, reconstructs the returned scores from each
record's endpointOutput, and writes a statistics + constraint-violations report to
/opt/ml/processing/output. A real Model Monitor compares against a baseline; this
minimal analyzer flags the one violation that matters locally, an empty capture.
"""

import glob
import json
import os

INPUT = "/opt/ml/processing/input/endpoint"
OUTPUT = "/opt/ml/processing/output"


def main():
    records, scores = 0, []
    for path in sorted(glob.glob(os.path.join(INPUT, "**", "*.jsonl"), recursive=True)):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                capture = json.loads(line)["captureData"]
                records += 1
                output = capture.get("endpointOutput", {}).get("data", "")
                for value in output.splitlines():
                    value = value.strip()
                    if not value:
                        continue
                    try:
                        scores.append(float(value))
                    except ValueError:
                        pass

    os.makedirs(OUTPUT, exist_ok=True)
    mean = sum(scores) / len(scores) if scores else 0.0
    with open(os.path.join(OUTPUT, "statistics.json"), "w") as fh:
        json.dump(
            {"num_records": records, "num_scores": len(scores), "mean_score": mean}, fh
        )

    violations = []
    if records == 0:
        violations.append(
            {
                "feature_name": "__dataset__",
                "constraint_check_type": "data_present",
                "description": "no captured records found",
            }
        )
    with open(os.path.join(OUTPUT, "constraint_violations.json"), "w") as fh:
        json.dump({"violations": violations}, fh)

    print(f"monitored {records} records, {len(scores)} scores, mean={mean:.4f}")


if __name__ == "__main__":
    main()
