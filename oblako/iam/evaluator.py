"""A compact IAM policy evaluator: identity authorization + role-trust evaluation.

Not a full IAM engine — it covers the common, demonstrable cases that moto's
control plane does not evaluate:

  * ``evaluate`` — given a principal's policy statements, decide Allow / Deny /
    ImplicitDeny for an action on a resource (string Action/Resource with ``*``
    and ``?`` wildcards; explicit Deny wins).
  * ``can_assume`` — evaluate a role's trust policy for ``sts:AssumeRole``,
    including cross-account ``...:root`` principals.

Conditions, NotAction/NotResource, and policy variables are intentionally out of
scope (nice-to-haves). moto holds the IAM state; this decides access.
"""

from __future__ import annotations

import fnmatch

ALLOW = "Allow"
DENY = "Deny"
IMPLICIT_DENY = "ImplicitDeny"


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _glob(pattern: str, value: str) -> bool:
    """IAM-style wildcard match (case-insensitive), supporting * and ?."""
    return pattern == "*" or fnmatch.fnmatchcase(value.lower(), pattern.lower())


def _action_matches(statement_action, action: str) -> bool:
    return any(_glob(p, action) for p in _as_list(statement_action))


def _resource_matches(statement_resource, resource: str) -> bool:
    patterns = _as_list(statement_resource)
    if not patterns:  # e.g. trust policies omit Resource
        return True
    return any(_glob(p, resource) for p in patterns)


def evaluate(statements, action: str, resource: str) -> str:
    """Return Allow, Deny, or ImplicitDeny for ``action`` on ``resource``.

    Explicit Deny always wins; otherwise a matching Allow grants; with neither it
    is an implicit deny (AWS default-deny).
    """
    decision = IMPLICIT_DENY
    for statement in statements:
        if not _action_matches(statement.get("Action"), action):
            continue
        if not _resource_matches(statement.get("Resource"), resource):
            continue
        effect = statement.get("Effect")
        if effect == "Deny":
            return DENY
        if effect == "Allow":
            decision = ALLOW
    return decision


def account_of(arn: str) -> str | None:
    """Extract the account id from an ARN (arn:aws:service:region:ACCOUNT:resource)."""
    parts = arn.split(":")
    return parts[4] if len(parts) > 4 and parts[4] else None


def _principal_matches(principal, principal_arn: str) -> bool:
    """Whether a trust-policy Principal block includes the principal or its account root."""
    if principal == "*":
        return True
    aws = principal.get("AWS") if isinstance(principal, dict) else None
    account = account_of(principal_arn)
    root = f"arn:aws:iam::{account}:root" if account else None
    for entry in _as_list(aws):
        if entry in ("*", principal_arn, account) or (root and entry == root):
            return True
    return False


def can_assume(trust_policy: dict, principal_arn: str) -> bool:
    """Evaluate a role trust policy for whether principal_arn may sts:AssumeRole it."""
    allowed = False
    for statement in _as_list(trust_policy.get("Statement")):
        if not _action_matches(statement.get("Action"), "sts:AssumeRole"):
            continue
        if not _principal_matches(statement.get("Principal"), principal_arn):
            continue
        if statement.get("Effect") == "Deny":
            return False
        if statement.get("Effect") == "Allow":
            allowed = True
    return allowed
