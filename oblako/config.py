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


def region() -> str:
    """Return the configured AWS region from OBLAKO_REGION, default us-east-1."""
    return os.environ.get("OBLAKO_REGION") or DEFAULT_REGION


def account_id() -> str:
    """Return the configured AWS account id (OBLAKO_ACCOUNT_ID, default 123456789012)."""
    return os.environ.get("OBLAKO_ACCOUNT_ID") or DEFAULT_ACCOUNT_ID


def arn(service: str, resource: str, *, region_scoped: bool = True) -> str:
    """Build an ARN for the configured account/region (region blank for global services like IAM)."""
    return f"arn:aws:{service}:{region() if region_scoped else ''}:{account_id()}:{resource}"
