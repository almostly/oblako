"""Pipeline processing step: generate the credit training data (stdlib only).

Runs in a stock python:3.11-slim container (no third-party deps) and writes the
labeled CSV (7 numeric + 5 categorical + is_bad) to the processing output channel.
"""

import csv
import math
import os
import random

OUT = "/opt/ml/processing/output"
NUMERIC = [
    "Application_Score",
    "Bureau_Score",
    "Loan_Amount",
    "Time_with_Bank",
    "Time_in_Employment",
    "Loan_to_income",
    "Gross_Annual_Income",
]
CATEGORICAL = [
    "Loan_Payment_Frequency",
    "Residential_Status",
    "Cheque_Card_Flag",
    "Existing_Customer_Flag",
    "Home_Telephone_Number",
]


def main():
    rng = random.Random(0)
    os.makedirs(OUT, exist_ok=True)
    cols = [*NUMERIC, *CATEGORICAL, "is_bad"]
    with open(os.path.join(OUT, "train.csv"), "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for _ in range(600):
            app, bureau = rng.uniform(300, 900), rng.uniform(300, 900)
            income, loan = rng.uniform(15_000, 200_000), rng.uniform(1_000, 60_000)
            l2i = round(loan / max(income, 1) * 100, 2)
            twb, tie = rng.uniform(0, 30), rng.uniform(0, 40)
            res = rng.choice(["Owner", "Tenant", "Living with parents"])
            risk = (
                -0.005 * app
                - 0.005 * bureau
                + 0.03 * l2i
                + (0.6 if res == "Tenant" else 0.0)
                - 0.03 * twb
                + 4.2
            )
            writer.writerow(
                {
                    "Application_Score": round(app),
                    "Bureau_Score": round(bureau),
                    "Loan_Amount": round(loan),
                    "Time_with_Bank": round(twb, 1),
                    "Time_in_Employment": round(tie, 1),
                    "Loan_to_income": l2i,
                    "Gross_Annual_Income": round(income),
                    "Loan_Payment_Frequency": rng.choice(
                        ["Monthly", "Weekly", "Fortnightly"]
                    ),
                    "Residential_Status": res,
                    "Cheque_Card_Flag": rng.choice(["Y", "N"]),
                    "Existing_Customer_Flag": rng.choice(["Y", "N"]),
                    "Home_Telephone_Number": rng.choice(["Y", "N"]),
                    "is_bad": 1 if rng.random() < 1 / (1 + math.exp(-risk)) else 0,
                }
            )
    print("wrote", os.path.join(OUT, "train.csv"))


if __name__ == "__main__":
    main()
