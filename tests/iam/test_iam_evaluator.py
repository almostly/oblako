"""Unit tests for the compact IAM policy evaluator."""

from oblako.iam import evaluator
from oblako.iam.evaluator import ALLOW, DENY, IMPLICIT_DENY


def test_allow_via_wildcard_action():
    stmts = [{"Effect": "Allow", "Action": "s3:*", "Resource": "arn:aws:s3:::bucket/*"}]
    assert evaluator.evaluate(stmts, "s3:GetObject", "arn:aws:s3:::bucket/key") == ALLOW


def test_implicit_deny_when_no_match():
    stmts = [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::other/*"}]
    assert evaluator.evaluate(stmts, "s3:GetObject", "arn:aws:s3:::bucket/key") == IMPLICIT_DENY


def test_explicit_deny_wins_over_allow():
    stmts = [
        {"Effect": "Allow", "Action": "*", "Resource": "*"},
        {"Effect": "Deny", "Action": "s3:DeleteObject", "Resource": "*"},
    ]
    assert evaluator.evaluate(stmts, "s3:DeleteObject", "arn:aws:s3:::b/k") == DENY
    assert evaluator.evaluate(stmts, "s3:GetObject", "arn:aws:s3:::b/k") == ALLOW


def test_action_match_is_case_insensitive():
    stmts = [{"Effect": "Allow", "Action": "dynamodb:getitem", "Resource": "*"}]
    assert evaluator.evaluate(stmts, "dynamodb:GetItem", "*") == ALLOW


def test_account_of():
    assert evaluator.account_of("arn:aws:iam::111111111111:user/alice") == "111111111111"
    assert evaluator.account_of("arn:aws:iam:::root") is None


def test_can_assume_same_principal():
    trust = {"Statement": [{
        "Effect": "Allow", "Action": "sts:AssumeRole",
        "Principal": {"AWS": "arn:aws:iam::111111111111:user/alice"},
    }]}
    assert evaluator.can_assume(trust, "arn:aws:iam::111111111111:user/alice") is True
    assert evaluator.can_assume(trust, "arn:aws:iam::111111111111:user/bob") is False


def test_can_assume_cross_account_root():
    # Account A's role trusts all of account B (B's root) -> any B principal may assume.
    trust = {"Statement": [{
        "Effect": "Allow", "Action": "sts:AssumeRole",
        "Principal": {"AWS": "arn:aws:iam::222222222222:root"},
    }]}
    assert evaluator.can_assume(trust, "arn:aws:iam::222222222222:role/scorer") is True
    assert evaluator.can_assume(trust, "arn:aws:iam::333333333333:role/x") is False


def test_can_assume_explicit_deny():
    trust = {"Statement": [
        {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"AWS": "*"}},
        {"Effect": "Deny", "Action": "sts:AssumeRole",
         "Principal": {"AWS": "arn:aws:iam::333333333333:root"}},
    ]}
    assert evaluator.can_assume(trust, "arn:aws:iam::222222222222:user/ok") is True
    assert evaluator.can_assume(trust, "arn:aws:iam::333333333333:user/no") is False
