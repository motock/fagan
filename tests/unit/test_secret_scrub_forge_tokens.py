"""Forge tokens never reach a dispatched agent's environment (FORGE-S3).

``pipeline.execution._strip_agent_secrets`` drops every name in
``_AGENT_SECRET_ENV`` from the child env. The GitHub tokens were already
covered; a GitLab forge adds ``GITLAB_TOKEN``, ``GLAB_TOKEN`` and
``CI_JOB_TOKEN``, which must be scrubbed the same way - a leaked token in an
agent's env is a credential leak, not a cosmetic one.
"""

from __future__ import annotations

import pytest

from pipeline import execution

_SENTINEL = "glpat-secret-value"
_PATH = "/usr/bin"


@pytest.mark.parametrize(
    "name", ["GITLAB_TOKEN", "GLAB_TOKEN", "CI_JOB_TOKEN"]
)
def test_forge_token_is_scrubbed(name):
    env = {name: _SENTINEL, "PATH": _PATH}

    scrubbed = execution._strip_agent_secrets(env)

    assert name not in scrubbed, f"{name} must not reach the agent env"
    assert _SENTINEL not in scrubbed.values()
    assert scrubbed["PATH"] == _PATH, "only secrets are removed"


def test_gitlab_token_scrubbed():
    scrubbed = execution._strip_agent_secrets(
        {"GITLAB_TOKEN": _SENTINEL, "PATH": _PATH}
    )

    assert "GITLAB_TOKEN" not in scrubbed
    assert scrubbed == {"PATH": _PATH}


def test_glab_token_scrubbed():
    scrubbed = execution._strip_agent_secrets(
        {"GLAB_TOKEN": _SENTINEL, "PATH": _PATH}
    )

    assert "GLAB_TOKEN" not in scrubbed
    assert scrubbed == {"PATH": _PATH}


def test_ci_job_token_scrubbed():
    scrubbed = execution._strip_agent_secrets(
        {"CI_JOB_TOKEN": _SENTINEL, "PATH": _PATH}
    )

    assert "CI_JOB_TOKEN" not in scrubbed
    assert scrubbed == {"PATH": _PATH}


def test_existing_github_tokens_are_still_scrubbed():
    """The extension must not drop the entries that were already covered."""
    env = {
        "GITHUB_TOKEN": _SENTINEL,
        "GH_TOKEN": _SENTINEL,
        "GITLAB_TOKEN": _SENTINEL,
        "GLAB_TOKEN": _SENTINEL,
        "CI_JOB_TOKEN": _SENTINEL,
        "PATH": _PATH,
    }

    assert execution._strip_agent_secrets(env) == {"PATH": _PATH}
