"""Central region/account configuration for oblako clients and ARNs.

oblako is single-backend, so the account is identity metadata, not isolated
storage — but ARNs, IAM, and STS should reflect a consistent account + region.
Both are read from the environment so a user can pick a region/account and have
every boto3 client oblako hands out use it:

    OBLAKO_REGION=eu-west-1 OBLAKO_ACCOUNT_ID=222222222222 oblako up

123456789012 is moto's default account, so IAM/STS ARNs line up with it.
"""

from __future__ import annotations

import os

DEFAULT_REGION = "us-east-1"
DEFAULT_ACCOUNT_ID = "123456789012"

# Common AWS regions offered in the dashboard region picker.
REGIONS = [
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "eu-west-1",
    "eu-west-2",
    "eu-central-1",
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "sa-east-1",
    "ca-central-1",
]

# Runtime overrides (set by the dashboard region picker); take precedence over env.
_region_override: str | None = None
_account_override: str | None = None


def region() -> str:
    """Return the active AWS region: runtime override, then OBLAKO_REGION, then default."""
    return _region_override or os.environ.get("OBLAKO_REGION") or DEFAULT_REGION


def account_id() -> str:
    """Return the active AWS account id: runtime override, then env, then default."""
    return (
        _account_override or os.environ.get("OBLAKO_ACCOUNT_ID") or DEFAULT_ACCOUNT_ID
    )


def set_region(value: str | None) -> None:
    """Set (or clear, with None) the runtime region override."""
    global _region_override
    _region_override = value or None


def set_account(value: str | None) -> None:
    """Set (or clear, with None) the runtime account override."""
    global _account_override
    _account_override = value or None


def arn(service: str, resource: str, *, region_scoped: bool = True) -> str:
    """Build an ARN for the configured account/region (region blank for global services like IAM)."""
    return f"arn:aws:{service}:{region() if region_scoped else ''}:{account_id()}:{resource}"
