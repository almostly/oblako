"""Local IAM + STS over moto's control plane, with oblako's policy evaluator.

moto stores users, roles, and policies (real boto3 ``iam``/``sts`` behavior);
oblako adds what moto does not evaluate: role trust-policy checks on AssumeRole
and identity-policy authorization simulation, both cross-account aware. The
account is identity metadata (single backend) — cross-account is expressed in the
ARNs and trust policies and decided by the evaluator.
"""

from __future__ import annotations

import json

from oblako.engines.iam import evaluator
from .boto import BotoService
from .moto import MotoService


class IamService(BotoService):
    """IAM/STS control plane (moto) plus oblako's trust + authorization evaluator."""

    aws_services = ("iam", "sts")

    name = "iam"

    def __init__(self, moto: MotoService | None = None):
        """Initialize against a moto control plane (shared instance if provided)."""
        self._moto = moto or MotoService()

    @property
    def endpoint_url(self) -> str:
        """Return the moto endpoint that backs the IAM/STS control plane."""
        return self._moto.endpoint_url

    def create_user(self, name: str) -> dict:
        """Create an IAM user and return its description."""
        return self.get_client().create_user(UserName=name)["User"]

    def create_role(self, name: str, trust_policy: dict) -> dict:
        """Create an IAM role with the given trust policy and return its description."""
        return self.get_client().create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
        )["Role"]

    def create_policy(self, name: str, document: dict) -> dict:
        """Create a customer-managed policy and return its description."""
        return self.get_client().create_policy(
            PolicyName=name,
            PolicyDocument=json.dumps(document),
        )["Policy"]

    def attach_role_policy(self, role_name: str, policy_arn: str) -> None:
        """Attach a managed policy to a role."""
        self.get_client().attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)

    def attach_user_policy(self, user_name: str, policy_arn: str) -> None:
        """Attach a managed policy to a user."""
        self.get_client().attach_user_policy(UserName=user_name, PolicyArn=policy_arn)

    @staticmethod
    def _statements(document) -> list:
        """Return a policy document's statements as a list."""
        if isinstance(document, dict):
            stmts = document.get("Statement", [])
            return stmts if isinstance(stmts, list) else [stmts]
        return []

    def gather_statements(self, principal_arn: str) -> list:
        """Collect all identity-policy statements (managed + inline) for a user/role ARN."""
        iam = self.get_client()
        name = principal_arn.split("/")[-1]
        is_role = ":role/" in principal_arn
        if is_role:
            attached = iam.list_attached_role_policies(RoleName=name)[
                "AttachedPolicies"
            ]
            inline_names = iam.list_role_policies(RoleName=name)["PolicyNames"]
            inline = [
                iam.get_role_policy(RoleName=name, PolicyName=p)["PolicyDocument"]
                for p in inline_names
            ]
        else:
            attached = iam.list_attached_user_policies(UserName=name)[
                "AttachedPolicies"
            ]
            inline_names = iam.list_user_policies(UserName=name)["PolicyNames"]
            inline = [
                iam.get_user_policy(UserName=name, PolicyName=p)["PolicyDocument"]
                for p in inline_names
            ]
        statements: list = []
        for ap in attached:
            pol = iam.get_policy(PolicyArn=ap["PolicyArn"])["Policy"]
            version = iam.get_policy_version(
                PolicyArn=ap["PolicyArn"],
                VersionId=pol["DefaultVersionId"],
            )["PolicyVersion"]
            statements += self._statements(version["Document"])
        for doc in inline:
            statements += self._statements(doc)
        return statements

    def authorize(self, principal_arn: str, action: str, resource: str) -> str:
        """Return Allow/Deny/ImplicitDeny for the principal doing action on resource."""
        return evaluator.evaluate(
            self.gather_statements(principal_arn), action, resource
        )

    def assume_role(
        self, role_arn: str, principal_arn: str, session_name: str = "oblako-session"
    ) -> dict:
        """Evaluate the role's trust policy, then sts:AssumeRole if the principal is allowed."""
        iam = self.get_client()
        role_name = role_arn.split("/")[-1]
        trust = iam.get_role(RoleName=role_name)["Role"]["AssumeRolePolicyDocument"]
        if not evaluator.can_assume(trust, principal_arn):
            return {
                "allowed": False,
                "reason": f"{principal_arn} is not permitted by the role's trust policy",
            }
        resp = self.get_client("sts").assume_role(
            RoleArn=role_arn, RoleSessionName=session_name
        )
        creds = resp["Credentials"]
        return {
            "allowed": True,
            "assumedRoleArn": resp["AssumedRoleUser"]["Arn"],
            "accessKeyId": creds["AccessKeyId"],
            "sessionToken": creds["SessionToken"][:28] + "…",
        }
