"""An agent's team_scope follows the two-segment namespace grammar (nf-02).

The agent scope is validated by the same ``is_segmented_namespace`` predicate
the store uses, so one grammar governs both. Every refusal names the failure
kind and never echoes the offending scope value.
"""

import pytest

from app.identity import IdentityError, Principal
from pipeline.namespace_names import is_segmented_namespace


def _agent(team_scope):
    return Principal("agent:x", principal_type="agent", team_scope=team_scope)


def test_agent_accepts_a_two_segment_scope_verbatim():
    principal = _agent("acme/team-1")

    assert principal.team_scope == "acme/team-1"


@pytest.mark.parametrize(
    "team_scope",
    [
        "acme/core.api",  # a dot is not part of the segment grammar
        "acme/core_api",  # an underscore is not part of the segment grammar
        "Acme/x",  # uppercase is refused
        "acme/x/y",  # three segments
        "acme/x\n",  # trailing newline must not slip past the match
        "/x",  # empty first segment
        "acme/",  # empty second segment
    ],
)
def test_agent_refuses_a_scope_outside_the_namespace_grammar(team_scope):
    with pytest.raises(IdentityError) as excinfo:
        _agent(team_scope)

    assert str(excinfo.value).startswith("invalid team scope")


@pytest.mark.parametrize(
    "team_scope",
    ["acme/core.api", "acme/core_api", "Acme/x", "acme/x/y", "acme/x\n", "/x", "acme/"],
)
def test_refusal_never_echoes_the_scope_value(team_scope):
    with pytest.raises(IdentityError) as excinfo:
        _agent(team_scope)

    assert team_scope not in str(excinfo.value)
    assert "acme" not in str(excinfo.value)


@pytest.mark.parametrize(
    "team_scope",
    [
        "acme/team-1",
        "acme/core.api",
        "acme/core_api",
        "Acme/x",
        "acme/x/y",
        "/x",
        "acme/",
    ],
)
def test_agent_scope_acceptance_matches_the_namespace_predicate(team_scope):
    accepted = True
    try:
        _agent(team_scope)
    except IdentityError:
        accepted = False

    assert accepted is is_segmented_namespace(team_scope)


def test_human_principal_with_no_scope_is_unchanged():
    principal = Principal("alice")

    assert principal.principal_type == "human"
    assert principal.team_scope == ""
