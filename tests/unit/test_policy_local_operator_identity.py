"""The local operator principal is shared with the identity layer (ADR 0009).

``app.policy`` must re-export ``app.identity.LOCAL_OPERATOR`` rather than build
its own copy: the enforcement plan will import the principal from the policy
module and run roles-based checks against it, so a roleless look-alike would
silently deny the local operator.
"""

import app.identity
import app.policy


def test_policy_local_operator_is_identity_local_operator():
    assert app.policy.LOCAL_OPERATOR is app.identity.LOCAL_OPERATOR


def test_policy_local_operator_has_all_roles_and_wildcard_scope():
    principal = app.policy.LOCAL_OPERATOR
    assert len(principal.roles) == 5 and "approver" in principal.roles and principal.scopes == ("*",)


def test_policy_local_operator_subject_matches_principal_subject():
    assert app.policy.LOCAL_OPERATOR_SUBJECT == app.policy.LOCAL_OPERATOR.subject


def test_local_policy_allows_plan_approve_for_local_operator_subject():
    assert app.policy.LocalPolicy().allow(app.identity.Principal(subject="local-operator"), app.policy.Action.PLAN_APPROVE) is True
