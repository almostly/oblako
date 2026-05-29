"""oblako IAM: a compact policy evaluator over moto's IAM/STS control plane."""

from .evaluator import ALLOW, DENY, IMPLICIT_DENY, account_of, can_assume, evaluate

__all__ = ["evaluate", "can_assume", "account_of", "ALLOW", "DENY", "IMPLICIT_DENY"]
