"""A tiny credit decision service: score, apply policy, return a decision.

A FastAPI app that takes a loan application, scores it, applies policy rules, and
returns approve / refer / decline with reason codes. No AWS dependency in the
logic, so the same image runs on a laptop, on oblako, or on ECS Fargate.

    uvicorn app:app --reload      # then POST to http://localhost:8000/decision
"""

from __future__ import annotations

import math
from enum import Enum

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="Credit decision service")


class Application(BaseModel):
    application_id: str
    age: int
    monthly_income: float
    requested_amount: float
    dti: float = Field(..., description="debt-to-income ratio, 0..1")
    utilization: float = Field(..., description="revolving utilization, 0..1")
    days_past_due: int = 0
    kyc_passed: bool = True


class Decision(str, Enum):
    approve = "APPROVE"
    refer = "REFER"
    decline = "DECLINE"


class DecisionResponse(BaseModel):
    application_id: str
    decision: Decision
    pd: float
    score: int
    reasons: list[str]


def probability_of_default(item: Application) -> float:
    z = -3.2 + 2.1 * item.utilization + 0.05 * item.days_past_due + 1.4 * item.dti
    return 1.0 / (1.0 + math.exp(-z))


APPROVE_BELOW, REFER_BELOW = 0.10, 0.20


def decide(item: Application) -> DecisionResponse:
    reasons: list[str] = []
    if not item.kyc_passed:
        reasons.append("KYC_FAILED")
    if item.age < 18:
        reasons.append("UNDER_MINIMUM_AGE")
    if item.dti > 0.50:
        reasons.append("DTI_TOO_HIGH")
    if item.requested_amount > 10 * item.monthly_income:
        reasons.append("AMOUNT_EXCEEDS_POLICY")

    pd = probability_of_default(item)
    score = round((1.0 - pd) * 1000)
    if reasons:
        decision = Decision.decline
    elif pd < APPROVE_BELOW:
        decision = Decision.approve
    elif pd < REFER_BELOW:
        decision = Decision.refer
        reasons.append("BORDERLINE_MANUAL_REVIEW")
    else:
        decision = Decision.decline
        reasons.append("SCORE_BELOW_CUTOFF")

    return DecisionResponse(
        application_id=item.application_id,
        decision=decision,
        pd=round(pd, 4),
        score=score,
        reasons=reasons,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}  # the ALB target-group health check hits this


@app.post("/decision", response_model=DecisionResponse)
def decision(application: Application) -> DecisionResponse:
    return decide(application)
