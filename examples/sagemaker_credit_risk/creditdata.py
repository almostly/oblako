"""Synthetic credit applications (7 numeric + 5 categorical) for the SageMaker example.

Matches the reference's feature schema (aws-samples/credit-risk-modeling-on-aws).
"""

import csv
import math
import random

NUMERIC = ["Application_Score", "Bureau_Score", "Loan_Amount", "Time_with_Bank",
           "Time_in_Employment", "Loan_to_income", "Gross_Annual_Income"]
CATEGORICAL = ["Loan_Payment_Frequency", "Residential_Status", "Cheque_Card_Flag",
               "Existing_Customer_Flag", "Home_Telephone_Number"]


def _row(rng):
    app = rng.uniform(300, 900)
    bureau = rng.uniform(300, 900)
    income = rng.uniform(15_000, 200_000)
    loan = rng.uniform(1_000, 60_000)
    l2i = round(loan / max(income, 1) * 100, 2)
    twb = rng.uniform(0, 30)
    tie = rng.uniform(0, 40)
    res = rng.choice(["Owner", "Tenant", "Living with parents"])
    row = {
        "Application_Score": round(app), "Bureau_Score": round(bureau),
        "Loan_Amount": round(loan), "Time_with_Bank": round(twb, 1),
        "Time_in_Employment": round(tie, 1), "Loan_to_income": l2i,
        "Gross_Annual_Income": round(income),
        "Loan_Payment_Frequency": rng.choice(["Monthly", "Weekly", "Fortnightly"]),
        "Residential_Status": res,
        "Cheque_Card_Flag": rng.choice(["Y", "N"]),
        "Existing_Customer_Flag": rng.choice(["Y", "N"]),
        "Home_Telephone_Number": rng.choice(["Y", "N"]),
    }
    # latent default risk: low scores + high loan-to-income + tenant + short tenure
    risk = (-0.005 * app - 0.005 * bureau + 0.03 * l2i + (0.6 if res == "Tenant" else 0.0)
            - 0.03 * twb + 4.2)
    row["is_bad"] = 1 if rng.random() < 1 / (1 + math.exp(-risk)) else 0
    return row


def write_training_csv(path, n=600, seed=0):
    """Write n labeled credit applications (12 features + `is_bad`) to a CSV."""
    rng = random.Random(seed)
    cols = [*NUMERIC, *CATEGORICAL, "is_bad"]
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for _ in range(n):
            writer.writerow(_row(rng))


def sample_applications():
    """Return a prime and a subprime applicant (all 12 features) to score."""
    return [
        {"Application_Score": 820, "Bureau_Score": 800, "Loan_Amount": 8000,
         "Time_with_Bank": 14, "Time_in_Employment": 12, "Loan_to_income": 8.0,
         "Gross_Annual_Income": 100000, "Loan_Payment_Frequency": "Monthly",
         "Residential_Status": "Owner", "Cheque_Card_Flag": "Y",
         "Existing_Customer_Flag": "Y", "Home_Telephone_Number": "Y"},
        {"Application_Score": 420, "Bureau_Score": 450, "Loan_Amount": 45000,
         "Time_with_Bank": 1, "Time_in_Employment": 0.5, "Loan_to_income": 150.0,
         "Gross_Annual_Income": 30000, "Loan_Payment_Frequency": "Weekly",
         "Residential_Status": "Tenant", "Cheque_Card_Flag": "N",
         "Existing_Customer_Flag": "N", "Home_Telephone_Number": "N"},
    ]
